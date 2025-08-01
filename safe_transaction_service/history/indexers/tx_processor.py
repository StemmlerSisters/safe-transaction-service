"""
Contains classes for processing indexed data and store Safe related models in database
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Sequence, Union

from django.db import transaction

from eth_typing import ChecksumAddress, HexStr
from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
from packaging.version import Version
from safe_eth.eth import EthereumClient, get_auto_ethereum_client
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.contracts import (
    get_safe_V1_0_0_contract,
    get_safe_V1_3_0_contract,
    get_safe_V1_4_1_contract,
)
from safe_eth.safe import SafeTx
from safe_eth.safe.safe_signature import SafeSignature, SafeSignatureApprovedHash
from safe_eth.util.util import to_0x_hex_str
from web3 import Web3
from web3.exceptions import Web3RPCError

from safe_transaction_service.account_abstraction.services import (
    AaProcessorService,
    get_aa_processor_service,
)
from safe_transaction_service.safe_messages import models as safe_message_models

from ..models import (
    EthereumTx,
    InternalTx,
    InternalTxDecoded,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeContractDelegate,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
)

logger = logging.getLogger(__name__)


class TxProcessorException(Exception):
    pass


class OwnerCannotBeRemoved(TxProcessorException):
    pass


class ModuleCannotBeDisabled(TxProcessorException):
    pass


class CannotFindPreviousTrace(TxProcessorException):
    pass


class UserOperationFailed(TxProcessorException):
    pass


class SafeTxProcessorProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            from django.conf import settings

            ethereum_client = get_auto_ethereum_client()
            ethereum_tracing_client = (
                EthereumClient(settings.ETHEREUM_TRACING_NODE_URL)
                if settings.ETHEREUM_TRACING_NODE_URL
                else None
            )

            if not ethereum_tracing_client:
                logger.warning("Ethereum tracing client was not configured")
            cls.instance = SafeTxProcessor(
                ethereum_client, ethereum_tracing_client, get_aa_processor_service()
            )
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class TxProcessor(ABC):
    @abstractmethod
    def process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        pass

    def process_decoded_transactions(
        self, internal_txs_decoded: Sequence[InternalTxDecoded]
    ) -> list[bool]:
        return [
            self.process_decoded_transaction(decoded_transaction)
            for decoded_transaction in internal_txs_decoded
        ]


class SafeTxProcessor(TxProcessor):
    """
    Processor for txs on Safe Contracts v0.0.1 - v1.0.0
    """

    def __init__(
        self,
        ethereum_client: EthereumClient,
        ethereum_tracing_client: Optional[EthereumClient],
        aa_processor_service: AaProcessorService,
    ):
        """
        :param ethereum_client: Used for regular RPC calls
        :param ethereum_tracing_client: Used for RPC calls requiring trace methods. It's required to get
           previous traces for a given `InternalTx` if not found on database
        :param aa_processor_service: Used for detecting and processing 4337 transactions
        """

        # This safe_tx_failure events allow us to detect a failed safe transaction
        self.ethereum_client = ethereum_client
        self.ethereum_tracing_client = ethereum_tracing_client
        self.aa_processor_service = aa_processor_service
        dummy_w3 = Web3()
        self.safe_tx_failure_events = [
            get_safe_V1_0_0_contract(dummy_w3).events.ExecutionFailed(),
            get_safe_V1_3_0_contract(dummy_w3).events.ExecutionFailure(),
            get_safe_V1_4_1_contract(dummy_w3).events.ExecutionFailure(),
        ]
        self.safe_tx_module_failure_events = [
            get_safe_V1_3_0_contract(dummy_w3).events.ExecutionFromModuleFailure(),
            get_safe_V1_4_1_contract(dummy_w3).events.ExecutionFromModuleFailure(),
        ]

        self.safe_tx_failure_events_topics = {
            event_abi_to_log_topic(event.abi) for event in self.safe_tx_failure_events
        }
        self.safe_tx_module_failure_topics = {
            event_abi_to_log_topic(event.abi)
            for event in self.safe_tx_module_failure_events
        }
        self.safe_last_status_cache: dict[str, SafeLastStatus] = {}
        self.signature_breaking_versions = (  # Versions where signing changed
            Version("1.0.0"),  # Safes >= 1.0.0 Renamed `baseGas` to `dataGas`
            Version("1.3.0"),  # ChainId was included
        )

    def clear_cache(self, safe_address: Optional[ChecksumAddress] = None) -> bool:
        """
        :param safe_address:
        :return: `True` if anything was deleted from cache, `False` otherwise
        """
        if safe_address:
            if result := (safe_address in self.safe_last_status_cache):
                del self.safe_last_status_cache[safe_address]
            return result
        else:
            self.safe_last_status_cache.clear()
            return True

    def is_failed(
        self, ethereum_tx: EthereumTx, safe_tx_hash: Union[HexStr, bytes]
    ) -> bool:
        """
        Detects failure events on a Safe Multisig Tx

        :param ethereum_tx:
        :param safe_tx_hash:
        :return: True if a Multisig Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in safe-eth-py, it doesn't belong here
        safe_tx_hash = HexBytes(safe_tx_hash)
        for log in ethereum_tx.logs:
            if (
                log["topics"]
                and log["data"]
                and HexBytes(log["topics"][0]) in self.safe_tx_failure_events_topics
            ):
                if (
                    len(log["topics"]) == 2
                    and HexBytes(log["topics"][1]) == safe_tx_hash
                ):
                    # On v1.4.1 safe_tx_hash is indexed, so it will be topic[1]
                    # event ExecutionFailure(bytes32 indexed txHash, uint256 payment);
                    return True
                elif HexBytes(log["data"])[:32] == safe_tx_hash:
                    # On v1.3.0 safe_tx_hash was not indexed, it was stored in the first 32 bytes, the rest is payment
                    # event ExecutionFailure(bytes32 txHash, uint256 payment);
                    return True
        return False

    def is_module_failed(
        self,
        ethereum_tx: EthereumTx,
        module_address: ChecksumAddress,
        safe_address: ChecksumAddress,
    ) -> bool:
        """
        Detects module failure events on a Safe Module Tx

        :param ethereum_tx:
        :param module_address:
        :param safe_address:
        :return: True if a Module Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in safe-eth-py, it doesn't belong here
        for log in ethereum_tx.logs:
            if (
                len(log["topics"]) == 2
                and (log["address"] == safe_address if "address" in log else True)
                and HexBytes(log["topics"][0]) in self.safe_tx_module_failure_topics
                and HexBytes(log["topics"][1])[-20:]
                == HexBytes(module_address)  # 20 bytes is an address size
            ):
                return True
        return False

    def get_safe_version_from_master_copy(
        self, master_copy: ChecksumAddress
    ) -> Optional[str]:
        """
        :param master_copy:
        :return: Safe version for master copy address
        """
        return SafeMasterCopy.objects.get_version_for_address(master_copy)

    def get_last_safe_status_for_address(
        self, address: ChecksumAddress
    ) -> Optional[SafeLastStatus]:
        try:
            safe_status = self.safe_last_status_cache.get(
                address
            ) or SafeLastStatus.objects.get_or_generate(address)
            return safe_status
        except SafeLastStatus.DoesNotExist:
            logger.error("[%s] SafeLastStatus not found", address)

    def is_version_breaking_signatures(
        self, old_safe_version: str, new_safe_version: str
    ) -> bool:
        """
        :param old_safe_version:
        :param new_safe_version:
        :return: `True` if migrating from a Master Copy old version to a new version breaks signatures,
        `False` otherwise
        """
        old_version = Version(
            Version(old_safe_version).base_version
        )  # Remove things like -alpha or +L2
        new_version = Version(Version(new_safe_version).base_version)
        if new_version < old_version:
            new_version, old_version = old_version, new_version
        for breaking_version in self.signature_breaking_versions:
            if old_version < breaking_version <= new_version:
                return True
        return False

    def swap_owner(
        self,
        internal_tx: InternalTx,
        safe_status: SafeStatus,
        owner: ChecksumAddress,
        new_owner: Optional[ChecksumAddress],
    ) -> None:
        """
        :param internal_tx:
        :param safe_status:
        :param owner:
        :param new_owner: If provided, `owner` will be replaced by `new_owner`. If not, `owner` will be removed
        :return:
        """
        contract_address = internal_tx._from
        if owner not in safe_status.owners:
            logger.error(
                "[%s] Error processing trace=%s with tx-hash=%s. Cannot remove owner=%s . "
                "Current owners=%s",
                contract_address,
                internal_tx.trace_address,
                internal_tx.ethereum_tx_id,
                owner,
                safe_status.owners,
            )
            raise OwnerCannotBeRemoved(
                f"Cannot remove owner {owner}. Current owners {safe_status.owners}"
            )

        if not new_owner:
            safe_status.owners.remove(owner)
            SafeContractDelegate.objects.remove_delegates_for_owner_in_safe(
                safe_status.address, owner
            )
        else:
            # Replace owner by new_owner in the same place of the list
            old_owners = list(safe_status.owners)
            safe_status.owners = [
                new_owner if current_owner == owner else current_owner
                for current_owner in safe_status.owners
            ]
            if old_owners != safe_status.owners:
                SafeContractDelegate.objects.remove_delegates_for_owner_in_safe(
                    safe_status.address, owner
                )
        MultisigConfirmation.objects.remove_unused_confirmations(
            contract_address, safe_status.nonce, owner
        )
        safe_message_models.SafeMessageConfirmation.objects.filter(owner=owner).delete()

    def disable_module(
        self,
        internal_tx: InternalTx,
        safe_status: SafeStatus,
        module: ChecksumAddress,
    ) -> None:
        """
        Disables a module for a Safe by removing it from the enabled modules list.

        :param internal_tx:
        :param safe_status:
        :param module:
        :return:
        :raises ModuleCannotBeRemoved: If the module is not in the list of enabled modules.
        """
        contract_address = internal_tx._from
        if module not in safe_status.enabled_modules:
            logger.error(
                "[%s] Error processing trace=%s with tx-hash=%s. Cannot disable module=%s . "
                "Current enabled modules=%s",
                contract_address,
                internal_tx.trace_address,
                internal_tx.ethereum_tx_id,
                module,
                safe_status.enabled_modules,
            )
            raise ModuleCannotBeDisabled(
                f"Cannot disable module {module}. Current enabled modules {safe_status.enabled_modules}"
            )

        safe_status.enabled_modules.remove(module)

    def store_new_safe_status(
        self, safe_last_status: SafeLastStatus, internal_tx: InternalTx
    ) -> SafeLastStatus:
        """
        Updates `SafeLastStatus`. An entry to `SafeStatus` is added too via a Django signal.

        :param safe_last_status:
        :param internal_tx:
        :return: Updated `SafeLastStatus`
        """
        safe_last_status.internal_tx = internal_tx
        safe_last_status.save()
        self.safe_last_status_cache[safe_last_status.address] = safe_last_status
        return safe_last_status

    @transaction.atomic
    def process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        contract_address = internal_tx_decoded.internal_tx._from
        self.clear_cache(safe_address=contract_address)
        try:
            processed_successfully = self.__process_decoded_transaction(
                internal_tx_decoded
            )
            internal_tx_decoded.set_processed()
        finally:
            self.clear_cache(safe_address=contract_address)
        return processed_successfully

    @transaction.atomic
    def process_decoded_transactions(
        self, internal_txs_decoded: Sequence[InternalTxDecoded]
    ) -> list[bool]:
        """
        Optimize to process multiple transactions in a batch

        :param internal_txs_decoded:
        :return: list of `True` if an element was processed correctly, `False` otherwise.
        """
        results: list[bool] = []
        if not internal_txs_decoded:
            return results

        internal_tx_ids = []
        contract_addresses = {
            internal_tx_decoded.internal_tx._from
            for internal_tx_decoded in internal_txs_decoded
        }
        banned_addresses = set(
            SafeContract.objects.get_banned_addresses(addresses=contract_addresses)
        )

        try:
            for internal_tx_decoded in internal_txs_decoded:
                contract_address = internal_tx_decoded.internal_tx._from
                internal_tx_ids.append(internal_tx_decoded.internal_tx_id)
                if contract_address in banned_addresses:
                    logger.info(
                        "Ignoring decoded internal txs for banned safe %s",
                        contract_address,
                    )
                    results.append(False)
                else:
                    try:
                        result = self.__process_decoded_transaction(internal_tx_decoded)
                        results.append(result)
                    except CannotFindPreviousTrace:
                        logger.critical(
                            "[%s] There's a problem with the RPC, it needs to be checked",
                            contract_address,
                        )
                        raise
                    except TxProcessorException:
                        logger.error(
                            "[%s] Problem processing internal txs for Safe, ignoring",
                            contract_address,
                        )
                        results.append(False)

            # Set all as decoded in the same batch
            InternalTxDecoded.objects.filter(internal_tx__in=internal_tx_ids).update(
                processed=True
            )
            return results
        finally:
            for contract_address in contract_addresses:
                self.clear_cache(safe_address=contract_address)

    def __process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        """
        Decode internal tx and creates needed models
        :param internal_tx_decoded: InternalTxDecoded to process. It will be set as `processed`
        :return: True if tx could be processed, False otherwise
        """
        internal_tx = internal_tx_decoded.internal_tx
        ethereum_tx = internal_tx.ethereum_tx
        contract_address = internal_tx._from
        function_name = internal_tx_decoded.function_name

        logger.debug(
            "[%s] Start processing InternalTxDecoded in tx-hash=%s function-name=%s",
            contract_address,
            to_0x_hex_str(HexBytes(internal_tx_decoded.internal_tx.ethereum_tx_id)),
            function_name,
        )

        if internal_tx.gas_used < 1000:
            # When calling a non existing function, fallback of the proxy does not return any error but we can detect
            # this kind of functions due to little gas used. Some of this transactions get decoded as they were
            # valid in old versions of the proxies, like changes to `setup`
            logger.debug(
                "[%s] Calling a function using no much gas %d, probably a non existing function, "
                "will not process it",
                contract_address,
                internal_tx.gas_used,
            )
            return False

        arguments = internal_tx_decoded.arguments
        master_copy = internal_tx.to
        processed_successfully = True

        if function_name == "setup" and contract_address != NULL_ADDRESS:
            # Index new Safes
            logger.debug("[%s] Processing Safe setup", contract_address)
            owners = arguments["_owners"]
            threshold = arguments["_threshold"]
            fallback_handler = arguments.get("fallbackHandler", NULL_ADDRESS)
            nonce = 0
            try:
                safe_contract: SafeContract = SafeContract.objects.get(
                    address=contract_address
                )
                if not safe_contract.ethereum_tx_id:
                    safe_contract.ethereum_tx = internal_tx.ethereum_tx
                    safe_contract.save(update_fields=["ethereum_tx"])
            except SafeContract.DoesNotExist:
                SafeContract.objects.create(
                    address=contract_address,
                    ethereum_tx=internal_tx.ethereum_tx,
                )
                logger.info("Found new Safe=%s", contract_address)

            self.store_new_safe_status(
                SafeLastStatus(
                    internal_tx=internal_tx,
                    address=contract_address,
                    owners=owners,
                    threshold=threshold,
                    nonce=nonce,
                    master_copy=master_copy,
                    fallback_handler=fallback_handler,
                ),
                internal_tx,
            )
        else:
            safe_last_status = self.get_last_safe_status_for_address(contract_address)
            if not safe_last_status:
                # Usually this happens from Safes coming from a not supported Master Copy
                # TODO When archive node is available, build SafeStatus from blockchain status
                logger.debug(
                    "[%s] Cannot process trace as `SafeLastStatus` is not found",
                    contract_address,
                )
                processed_successfully = False
            elif function_name in (
                "addOwnerWithThreshold",
                "removeOwner",
                "removeOwnerWithThreshold",
            ):
                logger.debug(
                    "[%s] Processing owner/threshold modification", contract_address
                )
                safe_last_status.threshold = (
                    arguments["_threshold"] or safe_last_status.threshold
                )  # Event doesn't have threshold
                owner = arguments["owner"]
                if function_name == "addOwnerWithThreshold":
                    safe_last_status.owners.insert(0, owner)
                else:  # removeOwner, removeOwnerWithThreshold
                    self.swap_owner(internal_tx, safe_last_status, owner, None)
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "swapOwner":
                logger.debug("[%s] Processing owner swap", contract_address)
                old_owner = arguments["oldOwner"]
                new_owner = arguments["newOwner"]
                self.swap_owner(internal_tx, safe_last_status, old_owner, new_owner)
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "changeThreshold":
                logger.debug("[%s] Processing threshold change", contract_address)
                safe_last_status.threshold = arguments["_threshold"]
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "changeMasterCopy":
                logger.debug("[%s] Processing master copy change", contract_address)
                # TODO Ban address if it doesn't have a valid master copy
                old_safe_version = self.get_safe_version_from_master_copy(
                    safe_last_status.master_copy
                )
                safe_last_status.master_copy = arguments["_masterCopy"]
                new_safe_version = self.get_safe_version_from_master_copy(
                    safe_last_status.master_copy
                )
                if (
                    old_safe_version
                    and new_safe_version
                    and self.is_version_breaking_signatures(
                        old_safe_version, new_safe_version
                    )
                ):
                    # Transactions queued not executed are not valid anymore
                    MultisigTransaction.objects.queued(contract_address).delete()
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "setFallbackHandler":
                logger.debug("[%s] Setting FallbackHandler", contract_address)
                safe_last_status.fallback_handler = arguments["handler"]
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "setGuard":
                safe_last_status.guard = (
                    arguments["guard"] if arguments["guard"] != NULL_ADDRESS else None
                )
                if safe_last_status.guard:
                    logger.debug("[%s] Setting Guard", contract_address)
                else:
                    logger.debug("[%s] Unsetting Guard", contract_address)
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "enableModule":
                logger.debug("[%s] Enabling Module", contract_address)
                safe_last_status.enabled_modules.append(arguments["module"])
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "disableModule":
                logger.debug("[%s] Disabling Module", contract_address)
                self.disable_module(internal_tx, safe_last_status, arguments["module"])
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name in {
                "execTransactionFromModule",
                "execTransactionFromModuleReturnData",
            }:
                logger.debug("[%s] Executing Tx from Module", contract_address)
                # TODO Add test with previous traces for processing a module transaction
                if "module" in arguments:
                    # L2 Safe with event SafeModuleTransaction indexed using events
                    module_address = arguments["module"]
                else:
                    # Regular Safe indexed using tracing
                    # Someone calls Module -> Module calls Safe Proxy -> Safe Proxy delegate calls Master Copy
                    # The trace that is being processed is the last one, so indexer needs to get the previous trace
                    try:
                        previous_trace = (
                            self.ethereum_tracing_client.tracing.get_previous_trace(
                                internal_tx.ethereum_tx_id,
                                internal_tx.trace_address_as_list,
                                skip_delegate_calls=True,
                            )
                        )
                    except Web3RPCError:
                        previous_trace = None

                    if not previous_trace:
                        message = (
                            f"[{contract_address}] Cannot find previous trace for "
                            f"tx-hash={to_0x_hex_str(HexBytes(internal_tx.ethereum_tx_id))} "
                            f"and trace-address={internal_tx.trace_address}"
                        )
                        logger.warning(message)
                        raise CannotFindPreviousTrace(message)
                    module_internal_tx = InternalTx.objects.build_from_trace(
                        previous_trace, internal_tx.ethereum_tx
                    )
                    module_address = (
                        module_internal_tx._from if module_internal_tx else NULL_ADDRESS
                    )
                failed = self.is_module_failed(
                    ethereum_tx, module_address, contract_address
                )
                module_data = HexBytes(arguments["data"])
                ModuleTransaction.objects.get_or_create(
                    internal_tx=internal_tx,
                    defaults={
                        "created": internal_tx.timestamp,
                        "safe": contract_address,
                        "module": module_address,
                        "to": arguments["to"],
                        "value": arguments["value"],
                        "data": module_data if module_data else None,
                        "operation": arguments["operation"],
                        "failed": failed,
                    },
                )
                SafeRelevantTransaction.objects.get_or_create(
                    ethereum_tx=ethereum_tx,
                    safe=contract_address,
                    defaults={"timestamp": internal_tx.timestamp},
                )
                # Detect 4337 UserOperations in this transaction
                number_detected_user_operations = (
                    self.aa_processor_service.process_aa_transaction(
                        contract_address, ethereum_tx
                    )
                )
                logger.debug(
                    "[%s] Detected %d 4337 transaction(s)",
                    contract_address,
                    number_detected_user_operations,
                )

            elif function_name == "approveHash":
                logger.debug("[%s] Processing hash approval", contract_address)
                multisig_transaction_hash = arguments["hashToApprove"]
                if "owner" in arguments:  # Event approveHash
                    owner = arguments["owner"]
                else:
                    previous_trace = (
                        self.ethereum_tracing_client.tracing.get_previous_trace(
                            internal_tx.ethereum_tx_id,
                            internal_tx.trace_address_as_list,
                            skip_delegate_calls=True,
                        )
                    )
                    if not previous_trace:
                        message = (
                            f"[{contract_address}] Cannot find previous trace for tx-hash={to_0x_hex_str(HexBytes(internal_tx.ethereum_tx_id))} and "
                            f"trace-address={internal_tx.trace_address}"
                        )
                        logger.warning(message)
                        raise CannotFindPreviousTrace(message)
                    previous_internal_tx = InternalTx.objects.build_from_trace(
                        previous_trace, internal_tx.ethereum_tx
                    )
                    owner = previous_internal_tx._from
                safe_signature = SafeSignatureApprovedHash.build_for_owner(
                    owner, multisig_transaction_hash
                )
                (multisig_confirmation, _) = MultisigConfirmation.objects.get_or_create(
                    multisig_transaction_hash=multisig_transaction_hash,
                    owner=owner,
                    defaults={
                        "created": internal_tx.timestamp,
                        "ethereum_tx": ethereum_tx,
                        "signature": safe_signature.export_signature(),
                        "signature_type": safe_signature.signature_type.value,
                    },
                )
                if not multisig_confirmation.ethereum_tx_id:
                    multisig_confirmation.ethereum_tx = ethereum_tx
                    multisig_confirmation.save(update_fields=["ethereum_tx"])
            elif function_name == "execTransaction":
                logger.debug("[%s] Processing transaction execution", contract_address)
                # Events for L2 Safes store information about nonce
                nonce = (
                    arguments["nonce"]
                    if "nonce" in arguments
                    else safe_last_status.nonce
                )
                if (
                    "baseGas" in arguments
                ):  # `dataGas` was renamed to `baseGas` in v1.0.0
                    base_gas = arguments["baseGas"]
                    safe_version = (
                        self.get_safe_version_from_master_copy(
                            safe_last_status.master_copy
                        )
                        or "1.3.0"
                    )
                else:
                    base_gas = arguments["dataGas"]
                    safe_version = "0.0.1"
                safe_tx = SafeTx(
                    None,
                    contract_address,
                    arguments["to"],
                    arguments["value"],
                    arguments["data"],
                    arguments["operation"],
                    arguments["safeTxGas"],
                    base_gas,
                    arguments["gasPrice"],
                    arguments["gasToken"],
                    arguments["refundReceiver"],
                    HexBytes(arguments["signatures"]),
                    safe_nonce=nonce,
                    safe_version=safe_version,
                    chain_id=self.ethereum_client.get_chain_id(),
                )
                safe_tx_hash = safe_tx.safe_tx_hash
                logger.debug(
                    "[%s] Processing transaction execution. nonce=%d safe-tx-hash=%s",
                    contract_address,
                    nonce,
                    to_0x_hex_str(safe_tx_hash),
                )

                failed = self.is_failed(ethereum_tx, safe_tx_hash)
                multisig_tx, _ = MultisigTransaction.objects.get_or_create(
                    safe_tx_hash=safe_tx_hash,
                    defaults={
                        "created": internal_tx.timestamp,
                        "safe": contract_address,
                        "ethereum_tx": ethereum_tx,
                        "to": safe_tx.to,
                        "value": safe_tx.value,
                        "data": safe_tx.data if safe_tx.data else None,
                        "operation": safe_tx.operation,
                        "safe_tx_gas": safe_tx.safe_tx_gas,
                        "base_gas": safe_tx.base_gas,
                        "gas_price": safe_tx.gas_price,
                        "gas_token": safe_tx.gas_token,
                        "refund_receiver": safe_tx.refund_receiver,
                        "nonce": safe_tx.safe_nonce,
                        "signatures": safe_tx.signatures,
                        "failed": failed,
                        "trusted": True,
                    },
                )
                SafeRelevantTransaction.objects.get_or_create(
                    ethereum_tx=ethereum_tx,
                    safe=contract_address,
                    defaults={"timestamp": internal_tx.timestamp},
                )

                # Don't modify created
                if not multisig_tx.ethereum_tx_id:
                    multisig_tx.ethereum_tx = ethereum_tx
                    multisig_tx.failed = failed
                    multisig_tx.signatures = HexBytes(arguments["signatures"])
                    multisig_tx.trusted = True
                    multisig_tx.save(
                        update_fields=["ethereum_tx", "failed", "signatures", "trusted"]
                    )

                for safe_signature in SafeSignature.parse_signature(
                    safe_tx.signatures, safe_tx_hash
                ):
                    (
                        multisig_confirmation,
                        _,
                    ) = MultisigConfirmation.objects.get_or_create(
                        multisig_transaction_hash=safe_tx_hash,
                        owner=safe_signature.owner,
                        defaults={
                            "created": internal_tx.timestamp,
                            "ethereum_tx": None,
                            "multisig_transaction": multisig_tx,
                            "signature": safe_signature.export_signature(),
                            "signature_type": safe_signature.signature_type.value,
                        },
                    )
                    if multisig_confirmation.signature != safe_signature.signature:
                        multisig_confirmation.signature = (
                            safe_signature.export_signature()
                        )
                        multisig_confirmation.signature_type = (
                            safe_signature.signature_type.value
                        )
                        multisig_confirmation.save(
                            update_fields=["signature", "signature_type"]
                        )

                safe_last_status.nonce = nonce + 1
                self.store_new_safe_status(safe_last_status, internal_tx)
            elif function_name == "execTransactionFromModule":
                logger.debug(
                    "[%s] Not processing execTransactionFromModule", contract_address
                )
                # No side effects or nonce increasing, but trace will be set as processed
            else:
                processed_successfully = False
                logger.warning(
                    "[%s] Cannot process InternalTxDecoded function_name=%s and arguments=%s",
                    contract_address,
                    function_name,
                    arguments,
                )
        logger.debug("[%s] End processing", contract_address)
        return processed_successfully
