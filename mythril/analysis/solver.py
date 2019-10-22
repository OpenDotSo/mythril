"""This module contains analysis module helpers to solve path constraints."""
from functools import lru_cache
from typing import Dict, List, Tuple, Union
from z3 import sat, unknown, FuncInterp
import z3

from mythril.analysis.analysis_args import analysis_args
from mythril.laser.ethereum.state.global_state import GlobalState
from mythril.laser.ethereum.state.constraints import Constraints
from mythril.laser.ethereum.keccak_function_manager import (
    keccak_function_manager,
    hash_matcher,
)
from mythril.laser.ethereum.transaction import BaseTransaction
from mythril.laser.smt import UGE, Optimize, symbol_factory
from mythril.laser.ethereum.time_handler import time_handler
from mythril.exceptions import UnsatError
from mythril.laser.ethereum.transaction.transaction_models import (
    ContractCreationTransaction,
)
import logging

log = logging.getLogger(__name__)


# LRU cache works great when used in powers of 2
@lru_cache(maxsize=2 ** 23)
def get_model(constraints, minimize=(), maximize=(), enforce_execution_time=True):
    """

    :param constraints:
    :param minimize:
    :param maximize:
    :param enforce_execution_time: Bool variable which enforces --execution-timeout's time
    :return:
    """
    s = Optimize()
    timeout = analysis_args.solver_timeout
    if enforce_execution_time:
        timeout = min(timeout, time_handler.time_remaining() - 500)
        if timeout <= 0:
            raise UnsatError
    s.set_timeout(timeout)
    for constraint in constraints:
        if type(constraint) == bool and not constraint:
            raise UnsatError

    constraints = [constraint for constraint in constraints if type(constraint) != bool]

    for constraint in constraints:
        s.add(constraint)
    for e in minimize:
        s.minimize(e)
    for e in maximize:
        s.maximize(e)
    result = s.check()
    if result == sat:
        return s.model()
    elif result == unknown:
        log.debug("Timeout encountered while solving expression using z3")
    raise UnsatError


def pretty_print_model(model):
    """ Pretty prints a z3 model

    :param model:
    :return:
    """
    ret = ""

    for d in model.decls():
        if type(model[d]) == FuncInterp:
            condition = model[d].as_list()
            ret += "%s: %s\n" % (d.name(), condition)
            continue

        try:
            condition = "0x%x" % model[d].as_long()
        except:
            condition = str(z3.simplify(model[d]))

        ret += "%s: %s\n" % (d.name(), condition)

    return ret


def get_transaction_sequence(
    global_state: GlobalState, constraints: Constraints
) -> Dict:
    """Generate concrete transaction sequence.

    :param global_state: GlobalState to generate transaction sequence for
    :param constraints: list of constraints used to generate transaction sequence
    """

    transaction_sequence = global_state.world_state.transaction_sequence

    concrete_transactions = []

    tx_constraints, minimize = _set_minimisation_constraints(
        transaction_sequence, constraints.copy(), [], 5000, global_state.world_state
    )
    try:
        model = get_model(tx_constraints, minimize=minimize)
    except UnsatError:
        raise UnsatError

    # Include creation account in initial state
    # Note: This contains the code, which should not exist until after the first tx
    initial_world_state = transaction_sequence[0].world_state
    initial_accounts = initial_world_state.accounts

    for transaction in transaction_sequence:
        concrete_transaction = _get_concrete_transaction(model, transaction)
        concrete_transactions.append(concrete_transaction)

    min_price_dict = {}  # type: Dict[str, int]
    for address in initial_accounts.keys():
        min_price_dict[address] = model.eval(
            initial_world_state.starting_balances[
                symbol_factory.BitVecVal(address, 256)
            ].raw,
            model_completion=True,
        ).as_long()

    concrete_initial_state = _get_concrete_state(initial_accounts, min_price_dict)
    if isinstance(transaction_sequence[0], ContractCreationTransaction):
        code = transaction_sequence[0].code
        _replace_with_actual_sha(concrete_transactions, model, code)
    else:
        _replace_with_actual_sha(concrete_transactions, model)
    steps = {"initialState": concrete_initial_state, "steps": concrete_transactions}

    return steps


def _replace_with_actual_sha(
    concrete_transactions: List[Dict[str, str]], model: z3.Model, code=None
):
    for tx in concrete_transactions:
        if hash_matcher not in tx["input"]:
            continue
        if code is not None and code.bytecode in tx["input"]:
            s_index = len(code.bytecode) + 10
        else:
            s_index = 10
        for i in range(s_index, len(tx["input"]), 64):
            data_slice = tx["input"][i : i + 64]
            if hash_matcher not in data_slice or len(data_slice) != 64:
                continue
            find_input = symbol_factory.BitVecVal(int(data_slice, 16), 256)
            input_ = None
            for size in keccak_function_manager.store_function:
                _, inverse = keccak_function_manager.get_function(size)
                try:
                    input_ = symbol_factory.BitVecVal(
                        model.eval(inverse(find_input).raw).as_long(), size
                    )
                except AttributeError:
                    continue
                hex_input = hex(input_.value)[2:]
                found = False
                for new_tx in concrete_transactions:
                    if hex_input in new_tx["input"]:
                        found = True
                        break
                if found:
                    break
            if input_ is None:
                continue
            keccak = keccak_function_manager.find_concrete_keccak(input_)
            hex_keccak = hex(keccak.value)
            if len(hex_keccak) != 66:
                hex_keccak = "0x" + "0" * (66 - len(hex_keccak)) + hex_keccak[2:]
            tx["input"] = tx["input"][:s_index] + tx["input"][s_index:].replace(
                tx["input"][i : 64 + i], hex_keccak[2:]
            )


def _get_concrete_state(initial_accounts: Dict, min_price_dict: Dict[str, int]):
    """ Gets a concrete state """
    accounts = {}
    for address, account in initial_accounts.items():
        # Skip empty default account

        data = dict()  # type: Dict[str, Union[int, str]]
        data["nonce"] = account.nonce
        data["code"] = account.code.bytecode
        data["storage"] = str(account.storage)
        data["balance"] = hex(min_price_dict.get(address, 0))
        accounts[hex(address)] = data
    return {"accounts": accounts}


def _get_concrete_transaction(model: z3.Model, transaction: BaseTransaction):
    """ Gets a concrete transaction from a transaction and z3 model"""
    # Get concrete values from transaction
    address = hex(transaction.callee_account.address.value)
    value = model.eval(transaction.call_value.raw, model_completion=True).as_long()
    caller = "0x" + (
        "%x" % model.eval(transaction.caller.raw, model_completion=True).as_long()
    ).zfill(40)

    input_ = ""
    if isinstance(transaction, ContractCreationTransaction):
        address = ""
        input_ += transaction.code.bytecode

    input_ += "".join(
        [
            hex(b)[2:] if len(hex(b)) % 2 == 0 else "0" + hex(b)[2:]
            for b in transaction.call_data.concrete(model)
        ]
    )

    # Create concrete transaction dict
    concrete_transaction = dict()  # type: Dict[str, str]
    concrete_transaction["input"] = "0x" + input_
    concrete_transaction["value"] = "0x%x" % value
    # Fixme: base origin assignment on origin symbol
    concrete_transaction["origin"] = caller
    concrete_transaction["address"] = "%s" % address

    return concrete_transaction


def _set_minimisation_constraints(
    transaction_sequence, constraints, minimize, max_size, world_state
) -> Tuple[Constraints, tuple]:
    """ Set constraints that minimise key transaction values

    Constraints generated:
    - Upper bound on calldata size
    - Minimisation of call value's and calldata sizes

    :param transaction_sequence: Transaction for which the constraints should be applied
    :param constraints: The constraints array which should contain any added constraints
    :param minimize: The minimisation array which should contain any variables that should be minimised
    :param max_size: The max size of the calldata array
    :return: updated constraints, minimize
    """
    for transaction in transaction_sequence:
        # Set upper bound on calldata size
        max_calldata_size = symbol_factory.BitVecVal(max_size, 256)
        constraints.append(UGE(max_calldata_size, transaction.call_data.calldatasize))

        # Minimize
        minimize.append(transaction.call_data.calldatasize)
        minimize.append(transaction.call_value)
        constraints.append(
            UGE(
                symbol_factory.BitVecVal(1000000000000000000000, 256),
                world_state.starting_balances[transaction.caller],
            )
        )

    for account in world_state.accounts.values():
        # Lazy way to prevent overflows and to ensure "reasonable" balances
        # Each account starts with less than 100 ETH
        constraints.append(
            UGE(
                symbol_factory.BitVecVal(100000000000000000000, 256),
                world_state.starting_balances[account.address],
            )
        )

    return constraints, tuple(minimize)
