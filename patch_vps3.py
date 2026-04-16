#!/usr/bin/env python3
"""Patch #3: Add USDC.e balance recovery system."""
import sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def patch(path, replacements):
    with open(path) as f:
        c = f.read()
    for old, new in replacements:
        if old not in c:
            print(f"  SKIP: pattern not found in {path}")
            continue
        c = c.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(c)
    print(f"  Patched {path}")

print("=== Patch 3: USDC.e Balance Recovery System ===")

# ── 1. Patch execution.py: add recovery methods ──
print("\n1. Patching core/execution.py...")
patch('core/execution.py', [
    # Add new methods after get_balance_usdc
    (
        '''        except Exception as exc:
            log.warning("balance query failed: %s", exc)
            return None
        return None''',
        '''        except Exception as exc:
            log.warning("balance query failed: %s", exc)
            return None
        return None

    async def get_onchain_usdc_balance(self, address: str) -> float:
        """Get USDC.e balance for any address via raw RPC."""
        import aiohttp
        addr_padded = address.lower().replace("0x", "").rjust(64, "0")
        data = "0x70a08231" + "0" * 24 + addr_padded[-40:]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                result = await self._rpc(session, "eth_call", [
                    {"to": self.USDC_E, "data": data}, "latest"
                ])
                return int(result, 16) / 1e6 if result and result != "0x" else 0.0
        except Exception as exc:
            log.warning("get_onchain_usdc_balance(%s) failed: %s", address[:10], exc)
            return 0.0

    async def cancel_all_open_orders(self) -> int:
        """Cancel all open CLOB orders to free locked USDC.e."""
        client = self._init_client()
        if client is None:
            return 0
        try:
            def _cancel():
                return client.cancel_all()
            resp = await asyncio.to_thread(_cancel)
            if resp:
                log.info("cancel_all_orders: %s", resp)
                return 1
            return 0
        except Exception as exc:
            log.warning("cancel_all_orders failed: %s", exc)
            return 0

    async def get_open_orders(self) -> list:
        """Get list of open orders from CLOB."""
        client = self._init_client()
        if client is None:
            return []
        try:
            def _get():
                return client.get_orders()
            orders = await asyncio.to_thread(_get)
            if orders:
                return [o for o in orders
                        if isinstance(o, dict)
                        and o.get("status") in ("live", "open")]
            return []
        except Exception as exc:
            log.warning("get_open_orders failed: %s", exc)
            return []

    async def full_balance_recovery(self) -> dict:
        """Comprehensive balance recovery: check all locations."""
        import aiohttp
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
        result = {
            "eoa_balance": 0.0, "proxy_balance": 0.0,
            "clob_balance": 0.0, "open_orders": 0,
            "recovered": 0.0, "actions": [],
        }

        try:
            eoa_bal = await self.get_onchain_usdc_balance(acct.address)
            result["eoa_balance"] = eoa_bal

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                proxy_addr = await self.get_proxy_wallet_address(session)
            if proxy_addr:
                proxy_bal = await self.get_onchain_usdc_balance(proxy_addr)
                result["proxy_balance"] = proxy_bal
                if proxy_bal > 0.01:
                    tx = await self.withdraw_proxy_usdc()
                    if tx:
                        result["recovered"] += proxy_bal
                        result["actions"].append(
                            f"Withdrew ${proxy_bal:.2f} from proxy: {tx}")

            clob_bal = await self.get_balance_usdc()
            if clob_bal is not None:
                result["clob_balance"] = clob_bal

            open_orders = await self.get_open_orders()
            result["open_orders"] = len(open_orders)
            if open_orders:
                await self.cancel_all_open_orders()
                result["actions"].append(
                    f"Canceled {len(open_orders)} open orders")
                await asyncio.sleep(3)
                if proxy_addr:
                    proxy_bal2 = await self.get_onchain_usdc_balance(proxy_addr)
                    if proxy_bal2 > 0.01:
                        tx = await self.withdraw_proxy_usdc()
                        if tx:
                            result["recovered"] += proxy_bal2
                            result["actions"].append(
                                f"Post-cancel proxy withdraw: ${proxy_bal2:.2f}")

            result["eoa_balance"] = await self.get_onchain_usdc_balance(acct.address)
        except Exception as exc:
            log.exception("full_balance_recovery failed: %s", exc)
            result["actions"].append(f"Error: {exc}")
        return result'''
    ),
])

# ── 2. Patch bot.py: more aggressive proxy sweep ──
print("\n2. Patching bot.py...")
patch('bot.py', [
    # Replace proxy_sweep_loop with more aggressive version
    (
        '''    async def _proxy_sweep_loop(self) -> None:
        """Periodically sweep USDC.e from proxy wallet to EOA.

        Polymarket may auto-redeem winning positions, depositing USDC.e
        into the proxy wallet. This background task ensures that money
        is transferred to the EOA every 3 minutes.
        """
        await asyncio.sleep(30)  # Initial delay
        while not self._stopping:
            try:
                tx = await self.executor.withdraw_proxy_usdc()
                if tx:
                    bal_str = ""
                    try:
                        usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
                        bal_str = f"\\n\\U0001f4b5 USDC.e: <b>${usdc_bal:,.2f}</b>"
                    except Exception:
                        pass
                    await self.notifier.send_text(
                        f"\\U0001f4b8 <b>Proxy sweep: USDC.e withdrawn to EOA</b>\\n"
                        f"TX: {tx_link_html(tx)}"
                        f"{bal_str}"
                    )
            except Exception as exc:
                log.debug("proxy sweep: %s", exc)
            await asyncio.sleep(180)  # Every 3 minutes''',
        '''    async def _proxy_sweep_loop(self) -> None:
        """Periodically sweep USDC.e from proxy wallet to EOA.

        Polymarket may auto-redeem winning positions, depositing USDC.e
        into the proxy wallet or CLOB exchange balance. This background
        task ensures money is recovered every 60 seconds.
        """
        await asyncio.sleep(20)  # Initial delay
        while not self._stopping:
            try:
                # 1. Sweep proxy wallet USDC.e
                tx = await self.executor.withdraw_proxy_usdc()
                if tx:
                    bal_str = ""
                    try:
                        usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
                        bal_str = f"\\n\\U0001f4b5 USDC.e: <b>${usdc_bal:,.2f}</b>"
                    except Exception:
                        pass
                    await self.notifier.send_text(
                        f"\\U0001f4b8 <b>Proxy sweep: USDC.e withdrawn to EOA</b>\\n"
                        f"TX: {tx_link_html(tx)}"
                        f"{bal_str}"
                    )

                # 2. Cancel stale open orders (free locked USDC)
                try:
                    open_orders = await self.executor.get_open_orders()
                    if open_orders:
                        log.info("sweep: found %d open orders — canceling",
                                 len(open_orders))
                        await self.executor.cancel_all_open_orders()
                        await asyncio.sleep(3)
                        tx2 = await self.executor.withdraw_proxy_usdc()
                        if tx2:
                            bal_str = ""
                            try:
                                usdc_bal, _, _ = await fetch_all_usdc(
                                    config.POLYGON_PUBLIC_KEY)
                                bal_str = f"\\n\\U0001f4b5 USDC.e: <b>${usdc_bal:,.2f}</b>"
                            except Exception:
                                pass
                            await self.notifier.send_text(
                                f"\\U0001f4b8 <b>Post-cancel sweep: USDC.e to EOA</b>\\n"
                                f"TX: {tx_link_html(tx2)}"
                                f"{bal_str}"
                            )
                except Exception as exc:
                    log.debug("sweep cancel-orders: %s", exc)

            except Exception as exc:
                log.debug("proxy sweep: %s", exc)
            await asyncio.sleep(60)  # Every 60 seconds (was 180)'''
    ),
    # Update auto-redeem fallback
    (
        '''        # All redeem attempts failed — try proxy USDC withdrawal as fallback
        try:
            withdraw_tx = await self.executor.withdraw_proxy_usdc()''',
        '''        # All redeem attempts failed — comprehensive recovery fallback
        log.info("auto-redeem: all 5 attempts failed — running full recovery")
        try:
            await self.executor.cancel_all_open_orders()
            await asyncio.sleep(3)
            withdraw_tx = await self.executor.withdraw_proxy_usdc()'''
    ),
    # Update the final warning message
    (
        '''        log.warning("auto-redeem failed after 5 attempts for condition %s", condition_id[:16])
        await self.notifier.send_text(
            f"\\u26a0\\ufe0f Auto-redeem failed after 5 attempts.\\n"
            f"Condition: <code>{condition_id[:20]}</code>\\n"
            f"Try /redeem or run: python3 scripts/redeem_all.py"
        )''',
        '''        try:
            from eth_account import Account
            acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
            eoa_bal = await self.executor.get_onchain_usdc_balance(acct.address)
            log.info("auto-redeem fallback: EOA balance = $%.2f", eoa_bal)
        except Exception:
            pass

        log.warning("auto-redeem failed after 5 attempts for condition %s", condition_id[:16])
        await self.notifier.send_text(
            f"\\u26a0\\ufe0f Auto-redeem failed after 5 attempts.\\n"
            f"Condition: <code>{condition_id[:20]}</code>\\n"
            f"Proxy sweep runs every 60s \\u2014 money will be recovered automatically.\\n"
            f"Or try /redeem manually."
        )'''
    ),
])

# ── 3. Patch telegram.py: add /recover command ──
print("\n3. Patching utils/telegram.py...")
patch('utils/telegram.py', [
    # Add command handler
    (
        '            "/exit": self._cmd_exit,\n            "/close": self._cmd_exit,',
        '            "/exit": self._cmd_exit,\n            "/close": self._cmd_exit,\n            "/recover": self._cmd_recover,\n            "/sweep": self._cmd_recover,'
    ),
    # Add callback handler
    (
        '        elif data == "nav:exit":\n            await self._cmd_exit([], chat_id)\n\n        # Mode toggle flow',
        '        elif data == "nav:exit":\n            await self._cmd_exit([], chat_id)\n        elif data == "nav:recover":\n            await self._cmd_recover([], chat_id)\n\n        # Mode toggle flow'
    ),
    # Add command menu entry
    (
        '            {"command": "redeem", "description": "Claim winning positions',
        '            {"command": "recover", "description": "Find & recover all USDC.e"},\n            {"command": "redeem", "description": "Claim winning positions'
    ),
    # Add help text
    (
        '            "/redeem',
        '            "/recover — Find & recover all USDC.e\\n"\n            "/redeem'
    ),
    # Add _cmd_recover method before _cmd_risk
    (
        '    async def _cmd_risk(self, args, chat_id):\n        snap = self.risk.snapshot()',
        '''    async def _cmd_recover(self, args, chat_id):
        """Run comprehensive balance recovery."""
        await self.notifier.send_text(
            "\\U0001f50d <b>BALANCE RECOVERY</b>\\nScanning all locations...",
            chat_id=chat_id,
        )
        try:
            result = await self.executor.full_balance_recovery()
            eoa = result.get("eoa_balance", 0)
            proxy = result.get("proxy_balance", 0)
            clob = result.get("clob_balance", 0)
            orders = result.get("open_orders", 0)
            recovered = result.get("recovered", 0)
            actions = result.get("actions", [])

            actions_text = ""
            if actions:
                actions_text = "\\n".join(f"  - {a}" for a in actions)
                actions_text = f"\\n\\n<b>Actions taken:</b>\\n{actions_text}"

            text = (
                f"\\U0001f4b0 <b>RECOVERY REPORT</b>\\n"
                f"{BAR}\\n"
                f"EOA wallet: <b>${eoa:,.2f}</b>\\n"
                f"Proxy wallet: ${proxy:,.2f}\\n"
                f"CLOB exchange: ${clob:,.2f}\\n"
                f"Open orders: {orders}\\n"
                f"{BAR}\\n"
                f"Recovered: <b>${recovered:,.2f}</b>"
                f"{actions_text}"
            )
            kb = {"inline_keyboard": [
                [{"text": "\\U0001f504 Run Again", "callback_data": "nav:recover"}],
                [{"text": "\\U0001f3e0 Dashboard", "callback_data": "nav:dashboard"}],
            ]}
            await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)
        except Exception as exc:
            await self.notifier.send_text(
                f"\\u274c Recovery error: {str(exc)[:200]}", chat_id=chat_id
            )

    async def _cmd_risk(self, args, chat_id):
        snap = self.risk.snapshot()'''
    ),
])

# Verify syntax
import py_compile
for f in ['core/execution.py', 'bot.py', 'utils/telegram.py']:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  Syntax OK: {f}")
    except py_compile.PyCompileError as e:
        print(f"  SYNTAX ERROR: {e}")
        sys.exit(1)

print("\n=== DONE! Run: pm2 restart polymarket-bot ===")
