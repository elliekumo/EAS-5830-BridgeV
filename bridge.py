from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
#from web3.middleware import geth_poa_middleware
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
        #w3.middleware_onion.inject(geth_poa_middleware, layer=0)
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
        import time

        # --- Connect to chains and contracts ---
        w3_scan = connect_to(chain)
        me = get_contract_info(chain, contract_info)
        scan_contract = w3_scan.eth.contract(address=me["address"], abi=me["abi"])

        other_chain = "destination" if chain == "source" else "source"
        w3_send = connect_to(other_chain)
        other = get_contract_info(other_chain, contract_info)
        relay_contract = w3_send.eth.contract(address=other["address"], abi=other["abi"])

        # --- Load warden key from sk.txt ---
        sk_path = Path(__file__).parent.absolute() / "sk.txt"
        with open(sk_path, "r") as f:
            sk = f.readline().strip()
        acct = w3_send.eth.account.from_key(sk)

        # --- Helper: safe tx sender on opposite chain ---
        def send_tx(fn):
            nonce = w3_send.eth.get_transaction_count(acct.address)
            try:
                gas_est = fn.estimate_gas({'from': acct.address})
            except Exception:
                gas_est = 500_000
            tx = fn.build_transaction({
                'from': acct.address,
                'nonce': nonce,
                'gas': int(gas_est * 1.2),
                'gasPrice': w3_send.eth.gas_price,
                'chainId': w3_send.eth.chain_id
            })
            signed = w3_send.eth.account.sign_transaction(tx, acct.key)
            txh = w3_send.eth.send_raw_transaction(signed.rawTransaction)
            return txh.hex()

        latest = w3_scan.eth.block_number

        # Scan window and chunk size tuned to RPC limits
        DEPTH = 15000 if chain == 'source' else 4000  # Fuji can tolerate more than BSC
        CHUNK = 512 if chain == 'destination' else 1024  # smaller chunks for BSC
        start = max(latest - DEPTH, 0)
        end = latest

        # --- Build topics and ABI handlers ---
        if chain == 'source':
            # Source emits Deposit(address,address,uint256)
            event_abi = None
            for e in me["abi"]:
                if e.get("type") == "event" and e.get("name") == "Deposit":
                    event_abi = e
                    break
            if event_abi is None:
                print("Deposit ABI not found")
                return 0
            sig = Web3.keccak(text="Deposit(address,address,uint256)").hex()
            topics = [sig]

            print(f"Scanning SOURCE blocks {start}..{end} for Deposit")
            found = 0

            # Chunked get_logs
            cur = start
            while cur <= end:
                lo = cur
                hi = min(cur + CHUNK - 1, end)
                try:
                    logs = w3_scan.eth.get_logs({
                        "fromBlock": lo,
                        "toBlock": hi,
                        "address": me["address"],
                        "topics": topics
                    })
                except Exception as e:
                    print(f"Deposit fetch error [{lo}-{hi}]: {e}")
                    # small backoff to be gentle with RPC
                    time.sleep(0.5)
                    cur = hi + 1
                    continue

                for lg in logs:
                    evt = scan_contract.events.Deposit().process_log(lg)
                    token = evt['args']['token']
                    recipient = evt['args']['recipient']
                    amount = evt['args']['amount']
                    print(f"[Deposit] token={token} recipient={recipient} amount={amount} (blk {lg['blockNumber']})")

                    try:
                        txh = send_tx(relay_contract.functions.wrap(token, recipient, amount))
                        print(f"→ wrap() sent on destination: {txh}")
                        found += 1
                    except Exception as ex:
                        print(f"wrap() failed: {ex}")

                # throttle a bit for BSC infra; harmless on Fuji
                time.sleep(0.15)
                cur = hi + 1

            if found == 0:
                print("No Deposit events in the scan window.")
            return found

        else:
            # Destination emits Unwrap(address,address,address,address,uint256)
            event_abi = None
            for e in other["abi"]:  # we can also use scan_contract.abi; using me/other is equivalent
                if e.get("type") == "event" and e.get("name") == "Unwrap":
                    event_abi = e
                    break
            if event_abi is None:
                print("Unwrap ABI not found")
                return 0
            sig = Web3.keccak(text="Unwrap(address,address,address,address,uint256)").hex()
            topics = [sig]

            print(f"Scanning DESTINATION blocks {start}..{end} for Unwrap")
            found = 0

            cur = start
            while cur <= end:
                lo = cur
                hi = min(cur + CHUNK - 1, end)
                try:
                    logs = w3_scan.eth.get_logs({
                        "fromBlock": lo,
                        "toBlock": hi,
                        "address": me["address"],
                        "topics": topics
                    })
                except Exception as e:
                    print(f"Unwrap fetch error [{lo}-{hi}]: {e}")
                    time.sleep(0.75)  # BSC rate limit is stricter
                    cur = hi + 1
                    continue

                for lg in logs:
                    evt = scan_contract.events.Unwrap().process_log(lg)
                    underlying = evt['args']['underlying_token']
                    recipient = evt['args']['to']
                    amount = evt['args']['amount']
                    print(f"[Unwrap] underlying={underlying} to={recipient} amount={amount} (blk {lg['blockNumber']})")

                    try:
                        txh = send_tx(relay_contract.functions.withdraw(underlying, recipient, amount))
                        print(f"→ withdraw() sent on source: {txh}")
                        found += 1
                    except Exception as ex:
                        print(f"withdraw() failed: {ex}")

                time.sleep(0.25)
                cur = hi + 1

            if found == 0:
                print("No Unwrap events in the scan window.")
            return found




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
        #         print(f"[Unwrap] Underlying_token: {underlying_token}, To: {recipient}, Amount: {amount} (blk {event['blockNumber']})")
        #
        #         # Call withdraw on source
        #         try:
        #             txh = send_tx(contract_opposite.functions.withdraw(underlying_token, recipient, amount))
        #             print(f"-> withdraw() sent on {opposite_chain}: {txh}")
        #         except Exception as e:
        #             print(f"-> withdraw() failed: {e}")
        #


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
