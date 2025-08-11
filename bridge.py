from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
# from web3.middleware import geth_poa_middleware
from datetime import datetime
import json
import pandas as pd
from eth_utils import to_checksum_address
from time import sleep


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
        tx_hash = w3_send.eth.send_raw_transaction(signed.rawTransaction)

        try:
            w3_send.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        except Exception:
            #even if timeout, return hash so we can log it
            pass
        return tx_hash.hex()

    # latest = w3_scan.eth.block_number
    # from_block = max(latest - 12, 0)
    # to_block = latest

    seen = 0

    if chain == "source":
        # one call returns ALL Deposit events in the range
        try:
            ev_filter = scan_contract.events.Deposit.create_filter(
                from_block='latest', argument_filters={}
            )
            events = ev_filter.get_all_entries()
        except Exception as e:
            print(f"Deposit fetch failed: {getattr(e, 'args', e)}")
            return 0

        if not events:
            print("No Deposit events in window.")
            return 0

        for ev in events:
            token = to_checksum_address(ev['args']['token'])
            recipient = to_checksum_address(ev['args']['recipient'])
            amount = ev['args']['amount']
            print(f"[Deposit] token={token} recipient={recipient} amount={amount} blk={ev['blockNumber']}")
            try:
                txh = send_tx(send_contract.functions.wrap(token, recipient, amount))
                print(f"-> wrap() succeed: {txh}")
                seen += 1
            except Exception as e:
                print(f"-> wrap() failed: {getattr(e, 'args', e)}")

    else:
        # one call returns ALL Unwrap events in the range
        try:
            ev_filter = scan_contract.events.Unwrap.create_filter(
                from_block='latest', argument_filters={}
            )
            events = ev_filter.get_all_entries()
        except Exception as e:
            print(f"Unwrap fetch failed: {getattr(e, 'args', e)}")
            return 0

        if not events:
            print("No Unwrap events in window.")
            return 0

        for ev in events:
            underlying = to_checksum_address(ev['args']['underlying_token'])
            recipient = to_checksum_address(ev['args']['to'])
            amount = ev['args']['amount']
            print(f"[Unwrap] underlying={underlying} to={recipient} amount={amount} blk={ev['blockNumber']}")
            try:
                txh = send_tx(send_contract.functions.withdraw(underlying, recipient, amount))
                print(f"-> withdraw() succeed: {txh}")
                seen += 1
            except Exception as e:
                print(f"-> withdraw() failed: {getattr(e, 'args', e)}")

    if seen == 0:
        print("No matching events in window.")
    return seen


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
