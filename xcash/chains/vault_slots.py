from __future__ import annotations

import structlog
from django.db import IntegrityError
from django.db import transaction as db_transaction

from chains.models import TERMINAL_TX_TASK_STATUSES
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage

logger = structlog.get_logger()


def should_predeploy_on_address_exposure(
    *,
    chain: Chain,
    crypto,
) -> bool:
    """判断返回 VaultSlot 地址前是否必须部署合约。

    EVM 原生币入账必须依赖 receive() emit XcashNativeReceived 才能被系统识别；
    ERC20/TRC20 Transfer 可以先打到 CREATE2 预测地址，scanner 观察到入账后再部署。
    """
    if chain.type != ChainType.EVM:
        return False
    return getattr(crypto, "pk", None) == chain.native_coin.pk


def schedule_deploy_after_commit_if_needed(
    *,
    slot: VaultSlot,
    chain: Chain,
    crypto,
) -> None:
    if slot.is_deployed:
        return
    if not should_predeploy_on_address_exposure(
        chain=chain,
        crypto=crypto,
    ):
        return
    db_transaction.on_commit(lambda slot_pk=slot.pk: VaultSlot.schedule_deploy(slot_pk))


def ensure_deposit_address(
    *,
    chain: Chain,
    customer,
    crypto,
) -> str:
    validate_supported_chain(chain)
    backend = get_backend(chain)

    project = customer.project
    existing = VaultSlot.objects.filter(
        chain=chain,
        project=project,
        usage=VaultSlotUsage.DEPOSIT,
        customer=customer,
    ).first()
    if existing is not None:
        schedule_deploy_after_commit_if_needed(
            slot=existing,
            chain=chain,
            crypto=crypto,
        )
        return existing.address

    if not project.vault:
        raise RuntimeError(f"Project {customer.project_id} VaultSlot Vault 地址未配置")
    salt = VaultSlot.build_salt(
        chain_type=chain.type,
        usage=VaultSlotUsage.DEPOSIT,
        customer=customer,
    )
    slot_address = backend.predict_address(vault=project.vault, salt=salt)
    try:
        slot, created = VaultSlot.objects.get_or_create(
            chain=chain,
            project=project,
            usage=VaultSlotUsage.DEPOSIT,
            customer=customer,
            defaults={
                "address": slot_address,
                "salt": salt,
            },
        )
    except IntegrityError as exc:
        try:
            slot = VaultSlot.objects.get(
                chain=chain,
                project=project,
                usage=VaultSlotUsage.DEPOSIT,
                customer=customer,
            )
        except VaultSlot.DoesNotExist as not_exist_exc:
            raise exc from not_exist_exc
    else:
        if created:
            schedule_deploy_after_commit_if_needed(
                slot=slot,
                chain=chain,
                crypto=crypto,
            )
    return slot.address


def ensure_invoice_address(
    *,
    project,
    chain: Chain,
    invoice_index: int,
    crypto,
) -> str:
    validate_supported_chain(chain)
    backend = get_backend(chain)

    existing = VaultSlot.objects.filter(
        chain=chain,
        project=project,
        usage=VaultSlotUsage.INVOICE,
        invoice_index=invoice_index,
    ).first()
    if existing is not None:
        schedule_deploy_after_commit_if_needed(
            slot=existing,
            chain=chain,
            crypto=crypto,
        )
        return existing.address

    if not project.vault:
        raise RuntimeError(f"Project {project.pk} VaultSlot Vault 地址未配置")
    salt = VaultSlot.build_salt(
        chain_type=chain.type,
        usage=VaultSlotUsage.INVOICE,
        project_id=project.pk,
        invoice_index=invoice_index,
    )
    slot_address = backend.predict_address(vault=project.vault, salt=salt)
    try:
        slot, created = VaultSlot.objects.get_or_create(
            chain=chain,
            project=project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=invoice_index,
            defaults={
                "address": slot_address,
                "salt": salt,
            },
        )
    except IntegrityError as exc:
        try:
            slot = VaultSlot.objects.get(
                chain=chain,
                project=project,
                usage=VaultSlotUsage.INVOICE,
                invoice_index=invoice_index,
            )
        except VaultSlot.DoesNotExist as not_exist_exc:
            raise exc from not_exist_exc
    else:
        if created:
            schedule_deploy_after_commit_if_needed(
                slot=slot,
                chain=chain,
                crypto=crypto,
            )
    return slot.address


def schedule_deploy(slot_pk: int) -> TxTask | None:
    with db_transaction.atomic():
        slot = (
            VaultSlot.objects.select_for_update(of=("self",))
            .select_related(
                "chain",
                "project",
            )
            .get(pk=slot_pk)
        )
        # 并发 waiters 可能在首个事务更新 deploy_tx_task 前就已经发起
        # SELECT ... FOR UPDATE 并排队。拿到锁后必须重新读这个判重字段，
        # 否则会继续使用排队查询开始时的旧值，为同一 CREATE2 地址重复建任务。
        slot.refresh_from_db(fields=["deploy_tx_task", "is_deployed"])

        if slot.is_deployed:
            return None

        backend = get_backend(slot.chain)

        deploy_task = None
        if slot.deploy_tx_task_id is not None:
            deploy_task = TxTask.objects.get(pk=slot.deploy_tx_task_id)

        if backend.is_deployed_on_chain(chain=slot.chain, address=slot.address):
            mark_deployed(slot)
            return None

        if (
            deploy_task is not None
            and deploy_task.status not in TERMINAL_TX_TASK_STATUSES
        ):
            return deploy_task
        if deploy_task is not None and deploy_task.status == TxTaskStatus.CONFIRMED:
            return deploy_task

        if not slot.project.vault:
            raise RuntimeError(f"Project {slot.project_id} VaultSlot Vault 地址未配置")

        # 锁住 VaultSlot 本行后再创建任务，避免并发 on_commit 调度同时看到
        # deploy_tx_task 为空，从而为同一个 CREATE2 地址创建多笔部署交易。
        task = backend.create_deploy_tx_task(slot=slot)
        if isinstance(task, TxTask):
            VaultSlot.objects.filter(pk=slot.pk).update(deploy_tx_task=task)
        return task


def mark_deployed(slot: VaultSlot) -> bool:
    updated = VaultSlot.objects.filter(pk=slot.pk, is_deployed=False).update(
        is_deployed=True
    )
    if updated:
        slot.is_deployed = True
    return bool(updated)


def mark_deployed_by_task(tx_task: TxTask) -> bool:
    return bool(
        VaultSlot.objects.filter(
            deploy_tx_task=tx_task,
            is_deployed=False,
        ).update(is_deployed=True)
    )


def mark_deployed_if_on_chain_for_task(tx_task: TxTask) -> bool:
    slot = (
        VaultSlot.objects.select_related("chain")
        .filter(deploy_tx_task=tx_task)
        .first()
    )
    if slot is None:
        return False
    if slot.is_deployed:
        return True
    backend = get_backend(slot.chain)
    try:
        deployed = backend.is_deployed_on_chain(chain=slot.chain, address=slot.address)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VaultSlot 部署失败后链上状态检查失败",
            chain=slot.chain.code,
            vault_slot_id=slot.pk,
            tx_task_id=tx_task.pk,
            error=str(exc),
        )
        return False
    if not deployed:
        return False
    return mark_deployed(slot)


def schedule_collect_for_deposit(deposit_pk: int) -> VaultSlotCollectSchedule | None:
    from deposits.models import Deposit

    deposit = Deposit.objects.select_related(
        "customer",
        "transfer__chain",
        "transfer__crypto",
    ).get(pk=deposit_pk)
    transfer = deposit.transfer
    chain = transfer.chain
    crypto = transfer.crypto

    if crypto == chain.native_coin:
        return None

    try:
        slot = VaultSlot.objects.get(
            chain=chain,
            customer=deposit.customer,
            usage=VaultSlotUsage.DEPOSIT,
            address=transfer.to_address,
        )
    except VaultSlot.DoesNotExist as exc:
        raise RuntimeError(
            "VaultSlot 不存在："
            f"deposit_id={deposit.pk} chain={chain.code} "
            f"customer_id={deposit.customer_id} address={transfer.to_address}"
        ) from exc

    return schedule_collect_for_slot(chain=chain, crypto=crypto, slot=slot)


def schedule_collect_for_invoice(invoice_pk: int) -> VaultSlotCollectSchedule | None:
    from invoices.models import Invoice

    invoice = Invoice.objects.select_related(
        "project",
        "chain",
        "crypto",
    ).get(pk=invoice_pk)

    if invoice.chain_id is None or invoice.crypto_id is None or not invoice.pay_address:
        return None

    chain = invoice.chain
    crypto = invoice.crypto
    if crypto == chain.native_coin:
        return None

    try:
        slot = VaultSlot.objects.get(
            chain=chain,
            project=invoice.project,
            usage=VaultSlotUsage.INVOICE,
            address=invoice.pay_address,
        )
    except VaultSlot.DoesNotExist as exc:
        raise RuntimeError(
            "Invoice VaultSlot 不存在："
            f"invoice_id={invoice.pk} chain={chain.code} "
            f"project_id={invoice.project_id} address={invoice.pay_address}"
        ) from exc

    return schedule_collect_for_slot(chain=chain, crypto=crypto, slot=slot)


def schedule_collect_for_slot(
    *,
    chain: Chain,
    crypto,
    slot: VaultSlot,
) -> VaultSlotCollectSchedule | None:
    if not slot.project.auto_collect_enabled:
        return None
    if not crypto.address(chain):
        raise RuntimeError(
            f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 VaultSlot 归集"
        )

    return VaultSlotCollectSchedule.ensure_pending(
        chain=chain,
        vault_slot=slot,
        crypto=crypto,
    )


def create_collect_tx_task_for_slot(*, chain: Chain, crypto, slot: VaultSlot) -> TxTask:
    if not crypto.address(chain):
        raise RuntimeError(
            f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 VaultSlot 归集"
        )
    return get_backend(chain).create_collect_tx_task(chain=chain, crypto=crypto, slot=slot)


def can_create_collect_tx_task(*, chain: Chain, crypto, slot: VaultSlot) -> bool:
    return get_backend(chain).can_create_collect_tx_task(
        chain=chain,
        crypto=crypto,
        slot=slot,
    )


def validate_supported_chain(chain: Chain) -> None:
    if chain.type not in {ChainType.EVM, ChainType.TRON}:
        raise ValueError("VaultSlot 仅支持 EVM / Tron 链")


def get_backend(chain: Chain):
    if chain.type == ChainType.EVM:
        from evm import vault_slots

        return vault_slots
    if chain.type == ChainType.TRON:
        from tron import vault_slots

        return vault_slots
    raise ValueError("VaultSlot 仅支持 EVM / Tron 链")
