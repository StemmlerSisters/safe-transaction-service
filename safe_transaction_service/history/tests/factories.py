import datetime
from typing import Any

from django.utils import timezone

import factory
from eth_account import Account
from factory.django import DjangoModelFactory
from factory.fuzzy import FuzzyInteger
from hexbytes import HexBytes
from safe_eth.eth import get_auto_ethereum_client
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.utils import fast_keccak_text
from safe_eth.safe import Safe
from safe_eth.safe.safe_signature import SafeSignatureType
from safe_eth.util.util import to_0x_hex_str

from ..models import (
    ERC20Transfer,
    ERC721Transfer,
    EthereumBlock,
    EthereumTx,
    EthereumTxCallType,
    IndexingStatus,
    IndexingStatusType,
    InternalTx,
    InternalTxDecoded,
    InternalTxType,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    ProxyFactory,
    SafeContract,
    SafeContractDelegate,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
    TokenTransfer,
)


class IndexingStatusFactory(DjangoModelFactory):
    class Meta:
        model = IndexingStatus

    indexing_type = factory.fuzzy.FuzzyChoice([tag.value for tag in IndexingStatusType])
    block_number = 0


class EthereumBlockFactory(DjangoModelFactory):
    class Meta:
        model = EthereumBlock

    number = factory.Sequence(lambda n: n + 1)
    gas_limit = factory.fuzzy.FuzzyInteger(100000000, 200000000)
    gas_used = factory.fuzzy.FuzzyInteger(100000, 500000)
    timestamp = factory.LazyFunction(timezone.now)
    block_hash = factory.Sequence(
        lambda n: to_0x_hex_str(fast_keccak_text(f"block-{n}"))
    )
    parent_hash = factory.Sequence(
        lambda n: to_0x_hex_str(fast_keccak_text(f"block{n - 1}"))
    )


class EthereumTxFactory(DjangoModelFactory):
    class Meta:
        model = EthereumTx

    block = factory.SubFactory(EthereumBlockFactory)
    tx_hash = factory.Sequence(
        lambda n: to_0x_hex_str(fast_keccak_text(f"ethereum_tx_hash-{n}"))
    )
    _from = factory.LazyFunction(lambda: Account.create().address)
    gas = factory.fuzzy.FuzzyInteger(1000, 5000)
    gas_price = factory.fuzzy.FuzzyInteger(1, 100)
    max_fee_per_gas = None
    max_priority_fee_per_gas = None
    data = factory.Sequence(lambda n: HexBytes("%x" % (n + 1000)))
    nonce = factory.Sequence(lambda n: n)
    to = factory.LazyFunction(lambda: Account.create().address)
    value = factory.fuzzy.FuzzyInteger(0, 1000)
    logs = factory.LazyFunction(lambda: [])
    type = 0


class TokenTransfer(DjangoModelFactory):
    ethereum_tx = factory.SubFactory(EthereumTxFactory)
    timestamp = factory.SelfAttribute("ethereum_tx.block.timestamp")
    block_number = factory.SelfAttribute("ethereum_tx.block.number")
    log_index = factory.Sequence(lambda n: n)
    address = factory.LazyFunction(lambda: Account.create().address)
    _from = factory.LazyFunction(lambda: Account.create().address)
    to = factory.LazyFunction(lambda: Account.create().address)

    class Meta:
        model = TokenTransfer
        abstract = True

    @factory.post_generation
    def safe_relevant_tx(self, create, extracted, **kwargs):
        if not create:
            return
        ethereum_tx_id = self.ethereum_tx_id
        timestamp = self.ethereum_tx.block.timestamp
        SafeRelevantTransaction.objects.get_or_create(
            safe=self._from,
            ethereum_tx_id=ethereum_tx_id,
            defaults={"timestamp": timestamp},
        )
        SafeRelevantTransaction.objects.get_or_create(
            safe=self.to,
            ethereum_tx_id=ethereum_tx_id,
            defaults={"timestamp": timestamp},
        )


class ERC20TransferFactory(TokenTransfer):
    value = factory.fuzzy.FuzzyInteger(0, 1000)

    class Meta:
        model = ERC20Transfer


class ERC721TransferFactory(TokenTransfer):
    token_id = factory.fuzzy.FuzzyInteger(0, 1000)

    class Meta:
        model = ERC721Transfer


class InternalTxFactory(DjangoModelFactory):
    class Meta:
        model = InternalTx

    ethereum_tx = factory.SubFactory(EthereumTxFactory)
    timestamp = factory.SelfAttribute("ethereum_tx.block.timestamp")
    block_number = factory.SelfAttribute("ethereum_tx.block.number")
    _from = factory.LazyFunction(lambda: Account.create().address)
    gas = factory.fuzzy.FuzzyInteger(1000, 5000)
    data = factory.Sequence(lambda n: HexBytes("%x" % (n + 1000)))
    to = factory.LazyFunction(lambda: Account.create().address)
    value = factory.fuzzy.FuzzyInteger(0, 1000)
    gas_used = factory.fuzzy.FuzzyInteger(1000, 5000)
    contract_address = None
    code = None
    output = None
    refund_address = NULL_ADDRESS
    tx_type = InternalTxType.CALL.value
    call_type = EthereumTxCallType.CALL.value
    trace_address = factory.Sequence(str)
    error = None

    @factory.post_generation
    def safe_relevant_tx(self, create, extracted, **kwargs):
        if not create or not self.is_ether_transfer:
            return
        SafeRelevantTransaction.objects.get_or_create(
            safe=self.to,
            ethereum_tx_id=self.ethereum_tx_id,
            defaults={"timestamp": self.ethereum_tx.block.timestamp},
        )


class InternalTxDecodedFactory(DjangoModelFactory):
    class Meta:
        model = InternalTxDecoded

    class Params:
        fallback_handler = "0xd5D82B6aDDc9027B22dCA772Aa68D5d74cdBdF44"
        hash_to_approve = (
            "0x8aca9664752dbae36135fd0956c956fc4a370feeac67485b49bcd4b99608ae41"
        )
        master_copy = "0x34CfAC646f301356fAa8B21e94227e3583Fe3F5F"
        module = "0x32E2301B40f8CBE0da4683A60cfB6d3544afec8F"
        old_owner = "0x32E2301B40f8CBE0da4683A60cfB6d3544afec8F"
        owner = "0xbee99d1d38A3FBc03F3EB9339F2E119Ae8E513bA"
        threshold = 1
        transaction = {
            "to": "0xe5738C4cF66f7d288Ef4fe3CaBd678FfB39CFF8A",
            "data": "0x",
            "value": 2345000000000000,
            "baseGas": 0,
            "gasPrice": 0,
            "gasToken": "0x0000000000000000000000000000000000000000",
            "operation": 0,
            "safeTxGas": 0,
            "signatures": "0x0000000000000000000000002d8d6cafa6b8b7eed96c3711734d24df40c121e70000000000000"
            "00000000000000000000000000000000000000000000000000001",
            "refundReceiver": "0x0000000000000000000000000000000000000000",
        }
        module_transaction = {
            "to": "0x14Eac0051a9DcD04D1AaCfDc3606397F3d3ab94C",
            "data": "0xe318b52b000000000000",
            "value": 0,
            "operation": 0,
        }

    internal_tx = factory.SubFactory(InternalTxFactory)
    function_name = factory.fuzzy.FuzzyText(prefix="safe-", suffix="fn")
    processed = False

    @factory.lazy_attribute
    def arguments(self) -> dict[str, Any]:
        if self.function_name == "addOwnerWithThreshold":
            return {"owner": self.owner, "_threshold": self.threshold}
        elif self.function_name == "approveHash":
            return {"hashToApprove": self.hash_to_approve}
        elif self.function_name == "changeMasterCopy":
            return {"_masterCopy": self.master_copy}
        elif self.function_name == "changeThreshold":
            return {"_threshold": self.threshold}
        elif self.function_name == "disableModule":
            return {
                "module": self.module,
                "prevModule": "0x0000000000000000000000000000000000000001",
            }
        elif self.function_name == "enableModule":
            return {"module": self.module}
        elif self.function_name == "execTransactionFromModule":
            return self.module_transaction
        elif self.function_name == "execTransaction":
            return self.transaction
        elif self.function_name == "removeOwner":
            return {
                "owner": self.old_owner,
                "prevOwner": "0x0000000000000000000000000000000000000001",
                "_threshold": self.threshold,
            }
        elif self.function_name == "setFallbackHandler":
            return {"handler": self.fallback_handler}
        elif self.function_name == "setup":
            return {
                "to": "0x0000000000000000000000000000000000000000",
                "data": "0x",
                "_owners": [self.owner],
                "payment": 0,
                "_threshold": self.threshold,
                "paymentToken": "0x0000000000000000000000000000000000000000",
                "fallbackHandler": self.fallback_handler,
                "paymentReceiver": "0x0000000000000000000000000000000000000000",
            }
        elif self.function_name == "swapOwner":
            return {
                "newOwner": self.owner,
                "oldOwner": self.old_owner,
                "prevOwner": "0x0000000000000000000000000000000000000001",
            }
        else:
            return {}


class ModuleTransactionFactory(DjangoModelFactory):
    class Meta:
        model = ModuleTransaction

    created = factory.LazyAttribute(lambda o: o.internal_tx.timestamp)
    internal_tx = factory.SubFactory(InternalTxFactory)
    safe = factory.LazyFunction(lambda: Account.create().address)
    module = factory.LazyFunction(lambda: Account.create().address)
    to = factory.LazyFunction(lambda: Account.create().address)
    value = FuzzyInteger(low=0, high=10)
    data = factory.Sequence(lambda n: fast_keccak_text(f"module-tx-{n}"))
    operation = FuzzyInteger(low=0, high=1)
    failed = False

    @factory.post_generation
    def safe_relevant_tx(self, create, extracted, **kwargs):
        if not create:
            return
        SafeRelevantTransaction.objects.get_or_create(
            safe=self.safe,
            ethereum_tx_id=self.internal_tx.ethereum_tx_id,
            defaults={"timestamp": self.internal_tx.ethereum_tx.block.timestamp},
        )


class MultisigTransactionFactory(DjangoModelFactory):
    class Meta:
        model = MultisigTransaction

    safe_tx_hash = factory.Sequence(
        lambda n: to_0x_hex_str(fast_keccak_text(f"multisig-tx-{n}"))
    )
    safe = factory.LazyFunction(lambda: Account.create().address)
    proposer = None
    proposed_by_delegate = None
    ethereum_tx = factory.SubFactory(EthereumTxFactory)
    to = factory.LazyFunction(lambda: Account.create().address)
    value = FuzzyInteger(low=0, high=10)
    data = b""
    operation = 0
    safe_tx_gas = FuzzyInteger(low=400000, high=500000)
    base_gas = FuzzyInteger(low=200000, high=300000)
    gas_price = FuzzyInteger(low=1, high=10)
    gas_token = NULL_ADDRESS
    refund_receiver = NULL_ADDRESS
    signatures = b""
    nonce = factory.Sequence(lambda n: n)
    failed = False
    origin = factory.Faker("name")
    trusted = False

    @factory.post_generation
    def safe_relevant_tx(self, create, extracted, **kwargs):
        if not create or not self.ethereum_tx:
            return
        SafeRelevantTransaction.objects.get_or_create(
            safe=self.safe,
            ethereum_tx_id=self.ethereum_tx_id,
            defaults={"timestamp": self.ethereum_tx.block.timestamp},
        )

    @factory.post_generation
    def enable_safe_tx_hash_calculation(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            ethereum_client = get_auto_ethereum_client()
            safe = Safe(self.safe, ethereum_client)
            safe_tx = safe.build_multisig_tx(
                self.to,
                self.value,
                self.data,
                self.operation,
                self.safe_tx_gas,
                self.base_gas,
                self.gas_price,
                self.gas_token,
                self.refund_receiver,
                safe_nonce=self.nonce,
            )
            self.delete()
            self.safe_tx_hash = safe_tx.safe_tx_hash
            self.save()


class MultisigConfirmationFactory(DjangoModelFactory):
    class Meta:
        model = MultisigConfirmation

    ethereum_tx = factory.SubFactory(EthereumTxFactory)
    multisig_transaction = factory.SubFactory(MultisigTransactionFactory)
    multisig_transaction_hash = factory.Sequence(
        lambda n: to_0x_hex_str(fast_keccak_text(f"multisig-confirmation-tx-{n}"))
    )
    owner = factory.LazyFunction(lambda: Account.create().address)
    signature = None
    signature_type = SafeSignatureType.APPROVED_HASH.value

    @factory.post_generation
    def force_sign_with_account(self, create, extracted, **kwargs):
        """
        Calculates real signature for the given account
        """
        if not create:
            return

        if extracted:
            account = extracted
            self.owner = account.address
            self.multisig_transaction_hash = self.multisig_transaction.safe_tx_hash
            signature = account.unsafe_sign_hash(self.multisig_transaction_hash)[
                "signature"
            ]
            self.signature = signature
            self.save()


class SafeContractFactory(DjangoModelFactory):
    class Meta:
        model = SafeContract

    address = factory.LazyFunction(lambda: Account.create().address)
    ethereum_tx = factory.SubFactory(EthereumTxFactory)


class SafeContractDelegateFactory(DjangoModelFactory):
    class Meta:
        model = SafeContractDelegate

    safe_contract = factory.SubFactory(SafeContractFactory)
    delegate = factory.LazyFunction(lambda: Account.create().address)
    delegator = factory.LazyFunction(lambda: Account.create().address)
    label = factory.Faker("name")
    read = True
    write = True
    expiry_date = timezone.now() + datetime.timedelta(minutes=90)


class MonitoredAddressFactory(DjangoModelFactory):
    address = factory.LazyFunction(lambda: Account.create().address)
    initial_block_number = factory.LazyFunction(lambda: 0)
    tx_block_number = factory.LazyFunction(lambda: 0)


class ProxyFactoryFactory(MonitoredAddressFactory):
    class Meta:
        model = ProxyFactory


class SafeMasterCopyFactory(MonitoredAddressFactory):
    version = factory.Sequence(lambda n: f"1.0.{n}")
    deployer = factory.Faker("company")
    l2 = False

    class Meta:
        model = SafeMasterCopy


class SafeRelevantTransactionFactory(DjangoModelFactory):
    timestamp = factory.LazyFunction(timezone.now)
    ethereum_tx = factory.SubFactory(EthereumTxFactory)
    safe = factory.LazyFunction(lambda: Account.create().address)

    class Meta:
        model = SafeRelevantTransaction


class SafeLastStatusFactory(DjangoModelFactory):
    class Meta:
        model = SafeLastStatus

    internal_tx = factory.SubFactory(InternalTxFactory)
    address = factory.LazyFunction(lambda: Account.create().address)
    owners = factory.LazyFunction(lambda: [Account.create().address for _ in range(4)])
    threshold = FuzzyInteger(low=1, high=2)
    nonce = factory.Sequence(lambda n: n)
    master_copy = factory.LazyFunction(lambda: Account.create().address)
    fallback_handler = NULL_ADDRESS
    guard = NULL_ADDRESS
    enabled_modules = []


class SafeStatusFactory(SafeLastStatusFactory):
    class Meta:
        model = SafeStatus
