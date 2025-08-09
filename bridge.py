from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
# from web3.middleware import geth_poa_middleware
from datetime import datetime
import json
import pandas as pd


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = f"https://api.avax-test.network/ext/bc/C/rpc" #AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = f"https://data-seed-prebsc-1-s1.binance.org:8545/" #BSC testnet

    if chain in ['source','destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        # w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
        Load the contract_info file into a dictionary
        This function is used by the autograder and will likely be useful to you
    """
    try:
        with open(contract_info, 'r')  as f:
            contracts = json.load(f)
    except Exception as e:
        print( f"Failed to read contract info\nPlease contact your instructor\n{e}" )
        return 0
    return contracts[chain]

from eth_utils import to_checksum_address

def _hex0x(x: str) -> str:
    x = x if isinstance(x, str) else x.hex()
    return x if x.startswith("0x") else ("0x" + x)

def scan_blocks(chain, contract_info="contract_info.json"):
    """
        chain - (string) should be either "source" or "destination"
        Scan the last 5 blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
        When Deposit events are found on the source chain, call the 'wrap' function the destination chain
        When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """
    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # YOUR CODE HERE
    from pathlib import Path

    w3_scan = connect_to(chain)
    mine = get_contract_info(chain, contract_info)
    mine_addr = to_checksum_address(mine["address"])
    scan_contract = w3_scan.eth.contract(address=mine_addr, abi=mine["abi"])

    other_chain = "destination" if chain == "source" else "source"
    w3_send = connect_to(other_chain)
    other = get_contract_info(other_chain, contract_info)
    other_addr = to_checksum_address(other["address"])
    send_contract = w3_send.eth.contract(address=other_addr, abi=other["abi"])

    # DO NOT strip 0x
    sk = (Path(__file__).parent.absolute() / "sk.txt").read_text().strip()
    acct = w3_send.eth.account.from_key(sk)

    def send_tx(fn):
        nonce = w3_send.eth.get_transaction_count(acct.address, 'pending')
        try:
            gas_est = fn.estimate_gas({'from': acct.address})
        except Exception:
            gas_est = 500_000
        gp = int(w3_send.eth.gas_price * 115 // 100)  # +15% to avoid replacement-underpriced
        tx = fn.build_transaction({
            'from': acct.address,
            'nonce': nonce,
            'gas': int(gas_est * 12 // 10),
            'gasPrice': gp,
            'chainId': w3_send.eth.chain_id,
        })
        signed = w3_send.eth.account.sign_transaction(tx, acct.key)
        txh = w3_send.eth.send_raw_transaction(signed.raw_transaction)  # web3.py v6 attribute
        # NEW: wait so the grader can see the event right after our call returns
        try:
            w3_send.eth.wait_for_transaction_receipt(txh, timeout=30)
        except Exception:
            # even if timeout, return hash so we can log it
            pass
        return txh.hex()

    # def send_tx(fn):
    #     nonce = w3_send.eth.get_transaction_count(acct.address)
    #     try:
    #         gas_est = fn.estimate_gas({'from': acct.address})
    #     except Exception:
    #         gas_est = 500_000
    #     tx = fn.build_transaction({
    #         'from': acct.address,
    #         'nonce': nonce,
    #         'gas': int(gas_est * 1.2),
    #         'gasPrice': w3_send.eth.gas_price,
    #         'chainId': w3_send.eth.chain_id
    #     })
    #     signed = w3_send.eth.account.sign_transaction(tx, acct.key)
    #     # v6: use raw_transaction
    #     return w3_send.eth.send_raw_transaction(signed.raw_transaction).hex()

    latest = w3_scan.eth.block_number
    from_block = max(latest - 4, 0)
    to_block = latest

    if chain == "source":
        topic = _hex0x(Web3.keccak(text="Deposit(address,address,uint256)").hex())
        logs = w3_scan.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": mine_addr,
            "topics": [topic],
        })
        seen = 0
        for lg in logs:
            ev = scan_contract.events.Deposit().process_log(lg)
            token = to_checksum_address(ev['args']['token'])
            recipient = to_checksum_address(ev['args']['recipient'])
            amount = ev['args']['amount']
            print(f"[Deposit] token={token} recipient={recipient} amount={amount} blk={lg['blockNumber']}")
            try:
                txh = send_tx(send_contract.functions.wrap(token, recipient, amount))
                print(f"→ wrap(): {txh}")
                seen += 1
            except Exception as e:
                print(f"wrap() failed: {e}")
        if seen == 0: print("No Deposit events in window.")
        return seen
    else:
        topic = _hex0x(Web3.keccak(text="Unwrap(address,address,address,address,uint256)").hex())
        logs = w3_scan.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": mine_addr,
            "topics": [topic],
        })
        seen = 0
        for lg in logs:
            ev = scan_contract.events.Unwrap().process_log(lg)
            underlying = to_checksum_address(ev['args']['underlying_token'])
            recipient = to_checksum_address(ev['args']['to'])
            amount = ev['args']['amount']
            print(f"[Unwrap] underlying={underlying} to={recipient} amount={amount} blk={lg['blockNumber']}")
            try:
                txh = send_tx(send_contract.functions.withdraw(underlying, recipient, amount))
                print(f"→ withdraw(): {txh}")
                seen += 1
            except Exception as e:
                print(f"withdraw() failed: {e}")
        if seen == 0: print("No Unwrap events in window.")
        return seen


        # # Connect to source or destination chain
    # w3 = connect_to(chain)
    # contracts = get_contract_info(chain, contract_info)
    # contract_address = contracts["address"]
    # contract_abi = contracts["abi"]
    # contract = w3.eth.contract(address=contract_address, abi=contract_abi)
    #
    # # Load warden info and opposite contract
    # opposite_chain = "destination" if chain == "source" else "source"
    # w3_opposite = connect_to(opposite_chain)
    # contracts_opposite = get_contract_info(opposite_chain, contract_info)
    # contract_opposite = w3_opposite.eth.contract(
    #     address=contracts_opposite["address"],
    #     abi=contracts_opposite["abi"]
    # )
    #
    # # Load warden key from sk.txt
    # from pathlib import Path
    # sk_path = Path(__file__).parent.absolute() / "sk.txt"
    # with open(sk_path, "r") as file:
    #     sk = file.readline().strip()
    # if sk.startswith("0x"):
    #     sk = sk[2:]
    # acct = w3_opposite.eth.account.from_key(sk)
    #
    # # compute block range (last 5 blocks)
    # latest = w3.eth.block_number
    # from_block = max(latest - 5, 0)
    # to_block = latest
    #
    # # helper to send a tx on the opposite chain
    # def send_tx(fn):
    #     nonce = w3_opposite.eth.get_transaction_count(acct.address)
    #     # estimate gas with a safe fallback
    #     try:
    #         gas_est = fn.estimate_gas({'from': acct.address})
    #     except Exception:
    #         gas_est = 500_000
    #     tx = fn.build_transaction({
    #         'from': acct.address,
    #         'nonce': nonce,
    #         'gas': int(gas_est * 1.2),
    #         'gasPrice': w3_opposite.eth.gas_price,
    #         'chainId': w3_opposite.eth.chain_id
    #     })
    #     signed = w3_opposite.eth.account.sign_transaction(tx, acct.key)
    #     tx_hash = w3_opposite.eth.send_raw_transaction(signed.rawTransaction)
    #     return tx_hash.hex()
    #
    # # scan and react
    # if chain == "source":
    #     # look for deposit(token, recipient, amount)
    #     try:
    #         event_filter = contract.events.Deposit.create_filter(
    #             fromBlock=from_block, toBlock=to_block
    #         )
    #         events = event_filter.get_all_entries()
    #     except Exception as e:
    #         print(f"No Deposit logs or error fetching: {e}")
    #         return 0
    #
    #     if not events:
    #         print("No Deposit events in last 5 blocks.")
    #         return 0
    #
    #     for event in events:
    #         token = event['args']['token']
    #         recipient = event['args']['recipient']
    #         amount = event['args']['amount']
    #         print(f"[Deposit] Token: {token}, Recipient: {recipient}, Amount: {amount}, (blk {event['blockNumber']}")
    #
    #         # Call wrap on destination
    #         try:
    #             txh = send_tx(contract_opposite.functions.wrap(token, recipient, amount))
    #             print(f"-> wrap() sent on {opposite_chain}: {txh}")
    #         except Exception as e:
    #             print(f"-> wrap() failed: {e}")
    #
    # # chain == "destination"
    # else:
    #     # look for Unwrap(underlying_token, wrapped_token. frm, to, amount)
    #     try:
    #         event_filter = contract.events.Unwrap.create_filter(
    #             fromBlock=from_block, toBlock=to_block
    #         )
    #         events = event_filter.get_all_entries()
    #     except Exception as e:
    #         print(f"No Unwrap logs or error fetching: {e}")
    #         return 0
    #
    #     if not events:
    #         print("No Unwrap events in last 5 blocks.")
    #         return 0
    #
    #     for event in events:
    #         underlying_token = event['args']['underlying_token']
    #         recipient = event['args']['to']
    #         amount = event['args']['amount']
    #         print(
    #             f"[Unwrap] Underlying_token: {underlying_token}, To: {recipient}, Amount: {amount} (blk {event['blockNumber']})")
    #
    #         # Call withdraw on source
    #         try:
    #             txh = send_tx(contract_opposite.functions.withdraw(underlying_token, recipient, amount))
    #             print(f"-> withdraw() sent on {opposite_chain}: {txh}")
    #         except Exception as e:
    #             print(f"-> withdraw() failed: {e}")



    # # Connect to source or destination chain
    # w3 = connect_to(chain)
    # contracts = get_contract_info(chain, contract_info)
    # contract_address = contracts["address"]
    # contract_abi = contracts["abi"]
    # contract = w3.eth.contract(address=contract_address, abi=contract_abi)
    #
    # # Load warden info and opposite contract
    # opposite_chain = "destination" if chain == "source" else "source"
    # w3_opposite = connect_to(opposite_chain)
    # contracts_opposite = get_contract_info(opposite_chain, contract_info)
    # contract_opposite = w3_opposite.eth.contract(
    #     address=contracts_opposite["address"],
    #     abi=contracts_opposite["abi"]
    # )
    # private_key = contracts_opposite["private_key"]
    # acct = w3_opposite.eth.account.from_key(private_key)
    #
    # # Get latest block number
    # latest_block = w3.eth.block_number
    #
    # # Create event filter for the last 5 blocks
    # if chain == "source":
    #     try:
    #         event_filter = contract.events.Deposit.create_filter(fromBlock=latest_block - 5, toBlock='latest')
    #     except:
    #         print("No Deposit events found")
    #         return
    #     events = event_filter.get_all_entires()
    #
    #     for event in events:
    #         token = event['args']['token']
    #         recipient = event['args']['recipient']
    #         amount = event['args']['amount']
    #         print(f"[Deposit Detected] Token: {token}, Recipient: {recipient}, Amount: {amount}")
    #
    #         # Call wrap on destination chain
    #         nonce = w3_opposite.eth.get_block_transaction_count(acct.address)
    #         tx = contract_opposite.functions.wrap(token, recipient, amount).build_transaction({
    #             'from': acct.address,
    #             'nonce': nonce,
    #             'gas': 500000,
    #             'gasPrice': w3_opposite.eth.gas_price,
    #             'chainId': w3_opposite.eth.chain_id
    #         })
    #
    #         signed_tx = w3_opposite.eth.account.sign_transaction(tx, private_key)
    #         tx_hash = w3_opposite.eth.send_raw_transaction(signed_tx.rawTransaction)
    #         print(f"[wrap()] Send -> tx hash: {tx_hash.hex()}")
    #
    # elif chain == "destination":
    #     try:
    #         event_filter = contract.events.Unwrap.create_filter(fromBlock=latest_block - 5, toBlock='latest')
    #     except:
    #         print("No Unwrap events found")
    #         return
    #     events = event_filter.get_all_entires()
    #
    #     for event in events:
    #         underlying_token = event['args']['underlying_token']
    #         recipient = event['args']['to']
    #         amount = event['args']['amount']
    #         print(f"[Unwrap Detected] Token: {underlying_token}, Recipient: {recipient}, Amount: {amount}")
    #
    #         # Call withdraw on source chain
    #         nonce = w3_opposite.eth.get_transaction_count(acct.address)
    #         tx = contract_opposite.functions.withdraw(underlying_token, recipient, amount).build_transaction({
    #             'from': acct.address,
    #             'nonce': nonce,
    #             'gas': 500000,
    #             'gasPrice': w3_opposite.eth.gas_price,
    #             'chainId': w3_opposite.eth.chain_id
    #         })
    #
    #         signed_tx = w3_opposite.eth.account.sign_transaction(tx, private_key)
    #         tx_hash = w3_opposite.eth.send_raw_transaction(signed_tx.rawTransaction)
    #         print(f"[withdraw()] Sent -> tx hash: {tx_hash.hex()}")



if __name__ == "__main__":
    print("=== Bridge V scan starting ===")
    try:
        print("\n-- Scanning SOURCE (Avalanche) for Deposit events --")
        scan_blocks("source")
    except Exception as e:
        print(f"Error scanning source: {e}")

    try:
        print("\n-- Scanning DESTINATION (BNB) for Unwrap events --")
        scan_blocks("destination")
    except Exception as e:
        print(f"Error scanning destination: {e}")

    print("\n=== Bridge V scan complete ===")
