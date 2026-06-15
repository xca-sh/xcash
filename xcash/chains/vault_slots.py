from __future__ import annotations

import structlog
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone

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
    ERC20/TRC20 和 Tron 原生 TRX 可以先打到 CREATE2 预测地址，scanner 观察到
    入账后再部署。
    """
    if chain.type != ChainType.EVM:
        return False
    return is_chain_native_crypto(chain=chain, crypto=crypto)


def is_chain_native_crypto(*, chain: Chain, crypto) -> bool:
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

    vault_address = project.vault_address_for_chain_type(chain.type)
    if not vault_address:
        raise RuntimeError(
            f"Project {customer.project_id} {chain.type} 智能合约收款归集地址未配置"
        )
    salt = VaultSlot.build_salt(
        chain_type=chain.type,
        usage=VaultSlotUsage.DEPOSIT,
        customer=customer,
    )
    slot_address = backend.predict_address(
        chain=chain,
        vault=vault_address,
        salt=salt,
    )
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

    vault_address = project.vault_address_for_chain_type(chain.type)
    if not vault_address:
        raise RuntimeError(
            f"Project {project.pk} {chain.type} 智能合约收款归集地址未配置"
        )
    salt = VaultSlot.build_salt(
        chain_type=chain.type,
        usage=VaultSlotUsage.INVOICE,
        project_id=project.pk,
        invoice_index=invoice_index,
    )
    slot_address = backend.predict_address(
        chain=chain,
        vault=vault_address,
        salt=salt,
    )
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
        if deploy_task is not None and deploy_task.status == TxTaskStatus.SUCCEEDED:
            return deploy_task

        if not slot.project.vault_address_for_chain_type(slot.chain.type):
            raise RuntimeError(
                f"Project {slot.project_id} {slot.chain.type} 智能合约收款归集地址未配置"
            )

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
        expedite_pending_collects(slot_pk=slot.pk)
        db_transaction.on_commit(
            lambda slot_pk=slot.pk: reconcile_deployed_native_balance(slot_pk)
        )
    return bool(updated)


def reconcile_deployed_native_balance(slot_pk: int) -> bool:
    """EVM VaultSlot 部署确认后主动补扫部署前滞留的原生币余额。

    CREATE2 预测地址在部署前已收到原生币时，部署本身不会触发 receive() 事件；
    这里在部署确认后刷新链上余额并排队 collect(address(0))。余额读取放在
    mark_deployed 的 on_commit 回调里执行，避免在行锁事务内等待链上 RPC。
    """
    from chains.vault_slot_balances import refresh_vault_slot_balance_safely

    slot = VaultSlot.objects.select_related("chain").filter(pk=slot_pk).first()
    if slot is None:
        return False
    if slot.chain.type != ChainType.EVM:
        return False

    native_crypto = slot.chain.native_coin
    balance = refresh_vault_slot_balance_safely(
        slot=slot,
        crypto=native_crypto,
        reason="vault_slot_deploy_native_reconcile",
    )
    if balance is None or balance.value <= 0:
        return False

    try:
        schedule = VaultSlotCollectSchedule.ensure_pending_due_now(
            chain=slot.chain,
            vault_slot=slot,
            crypto=native_crypto,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VaultSlot 部署后发现原生币滞留，但排队归集失败",
            chain=slot.chain.code,
            vault_slot_id=slot.pk,
            crypto=getattr(native_crypto, "symbol", None),
            balance_value=str(balance.value),
            error=str(exc),
        )
        return False

    logger.warning(
        "VaultSlot 部署后发现原生币滞留，已排队归集",
        schedule_id=schedule.pk,
        chain=slot.chain.code,
        vault_slot_id=slot.pk,
        crypto=getattr(native_crypto, "symbol", None),
        balance_value=str(balance.value),
    )
    return True


def mark_deployed_by_task(tx_task: TxTask) -> bool:
    slot = VaultSlot.objects.filter(deploy_tx_task=tx_task).first()
    if slot is None:
        return False
    return mark_deployed(slot)


def expedite_pending_collects(*, slot_pk: int) -> int:
    """部署确认后把该槽位 pending 的归集计划拨到当前时间,免等退避窗口。

    未部署槽位的归集会被前置闸门 defer_retry 推迟(最长 10 分钟);部署一确认就把
    due_at 拨回当前,下一轮 execute_due 立即清扫。走「归集触发部署」路径时聚合
    窗口在部署调度前已自然走完,不受影响;仅 EVM 原生预部署路径上,若入账恰好
    出现在部署确认前的极窄窗口内,首笔归集会提前一次,代价只是一笔小额清扫。
    """
    return VaultSlotCollectSchedule.objects.filter(
        vault_slot_id=slot_pk,
        tx_task__isnull=True,
    ).update(due_at=timezone.now())


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
    # 原生币在 CryptoOnChain 里 address="" 是正常形态，不算「未部署」；只有非原生币
    # 缺合约地址才是真正的未配置，拒绝调度。原生币归集由 collect(address(0)) 承载。
    if not crypto.address(chain) and not is_chain_native_crypto(
        chain=chain,
        crypto=crypto,
    ):
        raise RuntimeError(
            f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 VaultSlot 归集"
        )

    return VaultSlotCollectSchedule.ensure_pending(
        chain=chain,
        vault_slot=slot,
        crypto=crypto,
    )


def create_collect_tx_task_for_slot(*, chain: Chain, crypto, slot: VaultSlot) -> TxTask:
    # 原生币在 CryptoOnChain 里 address="" 是正常形态，不算「未部署」；只有非原生币
    # 缺合约地址才是真正的未配置，拒绝调度。原生币归集由 collect(address(0)) 承载。
    if not crypto.address(chain) and not is_chain_native_crypto(
        chain=chain,
        crypto=crypto,
    ):
        raise RuntimeError(
            f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 VaultSlot 归集"
        )
    return get_backend(chain).create_collect_tx_task(chain=chain, crypto=crypto, slot=slot)


def can_create_collect_tx_task(*, chain: Chain, slot: VaultSlot) -> bool:
    """归集前置闸门:已部署放行;未部署先转入部署流,本轮归集延迟退避重试。

    归集的核心路径是「部署是归集的前置状态」:slot 未部署时不归集,改为触发
    schedule_deploy。schedule_deploy 自带行锁、deploy_tx_task 判重与链上已部署
    检查;若链上已有合约会当场翻转 is_deployed,此时本轮直接放行,不必再等下一个
    退避窗口。任何异常(RPC 故障、vault 未配置等)都按「暂不可归集」处理,由调度
    退避消化,不让单个槽位的故障打断整批调度。
    """
    if slot.is_deployed:
        return True
    try:
        schedule_deploy(slot.pk)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VaultSlot 归集前部署调度失败,本轮归集延迟",
            chain=chain.code,
            vault_slot_id=slot.pk,
            error=str(exc),
        )
        return False
    # schedule_deploy 的链上检查可能已当场把 is_deployed 翻为 True。
    slot.refresh_from_db(fields=["is_deployed"])
    return slot.is_deployed


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
