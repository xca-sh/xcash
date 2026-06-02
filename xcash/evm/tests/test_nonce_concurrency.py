import threading

from django.db import close_old_connections
from django.db import connections
from django.test import TransactionTestCase
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import TxTaskType
from chains.models import Wallet
from currencies.models import Crypto
from evm.intents import build_contract_call_intent
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmNonceConcurrencyTests(TransactionTestCase):
    """多线程并发创建 EvmTxTask，验证 nonce 分配的严格递增和互斥性。

    EVM 的 schedule(intent) 只做 nonce 分配和 DB 写入，
    不涉及 signer 或 RPC，因此无需 mock 外部依赖。
    """

    THREAD_COUNT = 5

    def setUp(self):
        self.native = Crypto.objects.create(
            name="Ethereum Concurrency",
            symbol="ETHCC",
            coingecko_id="ethereum-concurrency",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://localhost:8545",
        )
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x000000000000000000000000000000000000CC01"
            ),
        )

    def test_concurrent_contract_call_schedule_assigns_unique_sequential_nonces(self):
        """同一 (address, chain) 上 N 个线程同时 schedule(intent)，nonce 必须为 {0..N-1}。"""
        barrier = threading.Barrier(self.THREAD_COUNT)
        results: list[int] = []
        errors: list[Exception] = []
        recipient = Web3.to_checksum_address(
            "0x000000000000000000000000000000000000CC02"
        )

        def schedule(thread_idx: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                task = EvmTxTask.schedule(
                    build_contract_call_intent(
                        sender=self.address,
                        chain=self.chain,
                        contract_address=recipient,
                        data="0xdeadbeef",
                        gas=120_000,
                        tx_type=TxTaskType.VaultSlotCollect,
                        )
                )
                results.append(task.nonce)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=schedule, args=(i,))
            for i in range(self.THREAD_COUNT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertFalse(errors, f"线程异常: {errors}")
        self.assertEqual(len(results), self.THREAD_COUNT)
        self.assertEqual(sorted(results), list(range(self.THREAD_COUNT)))

        from chains.models import AddressChainState

        state = AddressChainState.objects.get(
            address=self.address, chain=self.chain
        )
        self.assertEqual(state.address, self.address)
        self.assertEqual(state.chain, self.chain)
        self.assertEqual(
            EvmTxTask.objects.filter(
                sender=self.address, chain=self.chain
            ).count(),
            self.THREAD_COUNT,
        )

    def test_concurrent_contract_call_schedule_across_addresses_are_independent(self):
        """不同地址并发 schedule，各自 nonce 独立从 0 开始。"""
        addresses = []
        for i in range(self.THREAD_COUNT):
            addr = Address.objects.create(
                wallet=self.wallet,
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                bip44_account=1,
                address_index=100 + i,
                address=Web3.to_checksum_address(
                    f"0x000000000000000000000000000000000000D{i:03d}"
                ),
            )
            addresses.append(addr)

        barrier = threading.Barrier(self.THREAD_COUNT)
        results: list[tuple[str, int]] = []
        errors: list[Exception] = []
        recipient = Web3.to_checksum_address(
            "0x000000000000000000000000000000000000CC02"
        )

        def schedule(addr: Address) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                task = EvmTxTask.schedule(
                    build_contract_call_intent(
                        sender=addr,
                        chain=self.chain,
                        contract_address=recipient,
                        data="0xdeadbeef",
                        gas=120_000,
                        tx_type=TxTaskType.VaultSlotCollect,
                        )
                )
                results.append((str(addr.address), task.nonce))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=schedule, args=(addr,))
            for addr in addresses
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertFalse(errors, f"线程异常: {errors}")
        self.assertEqual(len(results), self.THREAD_COUNT)
        for _addr_str, nonce in results:
            self.assertEqual(nonce, 0)

    def test_concurrent_1000_tasks_nonce_sequential_0_to_999(self):
        """1000 个线程并发创建任务，验证 nonce 严格从 0 递增到 999。

        同时验证数据库触发器 trg_evm_tx_task_nonce_sequential
        在高并发下与 AddressChainState 行锁协同工作，不会出现跳跃或重复。
        """
        task_count = 1000
        # 保持真实竞争窗口，但不要让测试一次性占满本地 Postgres 连接上限。
        batch_size = 10
        results: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()
        recipient = Web3.to_checksum_address(
            "0x000000000000000000000000000000000000CC02"
        )

        def schedule(batch_barrier: threading.Barrier) -> None:
            close_old_connections()
            try:
                batch_barrier.wait(timeout=10)
                task = EvmTxTask.schedule(
                    build_contract_call_intent(
                        sender=self.address,
                        chain=self.chain,
                        contract_address=recipient,
                        data="0xdeadbeef",
                        gas=120_000,
                        tx_type=TxTaskType.VaultSlotCollect,
                        )
                )
                with lock:
                    results.append(task.nonce)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        # 分批执行，每批线程同时起跑
        for batch_start in range(0, task_count, batch_size):
            current_batch = min(batch_size, task_count - batch_start)
            barrier = threading.Barrier(current_batch)
            threads = [
                threading.Thread(target=schedule, args=(barrier,))
                for _ in range(current_batch)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        self.assertFalse(errors, f"线程异常（共 {len(errors)} 个）: {errors[:5]}")
        self.assertEqual(len(results), task_count)

        # 核心断言：nonce 恰好是 {0, 1, 2, ..., 999}
        self.assertEqual(sorted(results), list(range(task_count)))

        # 数据库记录数一致
        db_count = EvmTxTask.objects.filter(
            sender=self.address, chain=self.chain
        ).count()
        self.assertEqual(db_count, task_count)

        # AddressChainState 只负责行锁，nonce 进度由 EvmTxTask 记录推导。
        from chains.models import AddressChainState

        state = AddressChainState.objects.get(
            address=self.address, chain=self.chain
        )
        self.assertEqual(state.address, self.address)
        self.assertEqual(state.chain, self.chain)

        # 数据库层面无空洞：max(nonce) == count - 1
        from django.db.models import Max

        max_nonce = EvmTxTask.objects.filter(
            sender=self.address, chain=self.chain
        ).aggregate(m=Max("nonce"))["m"]
        self.assertEqual(max_nonce, task_count - 1)
