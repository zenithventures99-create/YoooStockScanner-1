#!/usr/bin/env python3
"""
Project Artha V13.0 - The Final Multibagger Engine
Integrated with curl_cffi to bypass Cloudflare/Akamai blocks.
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest import mock

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests  # <--- THE BYPASS

# ----------------------------------------------------------------------------
# Configuration & Constants
# ----------------------------------------------------------------------------
TIMEOUT = 15  # Slightly increased to handle NSE's slow response times
STATE_FILE = "project-artha-state.json"
GITHUB_REPO = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ----------------------------------------------------------------------------
# 1. State Management (Safe & Immutable)
# ----------------------------------------------------------------------------
def load_state() -> Tuple[Set[str], Optional[str]]:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return set(), None
        
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            return set(), None
        resp.raise_for_status()
        data = resp.json()
        sha = data.get("sha")
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
        keys_list = json.loads(content)
        return set(keys_list), sha
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"State load failed: {e}")

def save_state(new_keys: Set[str]) -> bool:
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return False
        
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    
    for attempt in range(3):
        try:
            current_keys, current_sha = load_state()
        except Exception as e:
            logging.error(f"Failed to fetch current state during save attempt {attempt + 1}: {e}")
            return False
            
        all_keys = list(current_keys | new_keys)
        content_b64 = base64.b64encode(json.dumps(all_keys).encode()).decode()
        
        payload = {
            "message": "Update state",
            "content": content_b64,
        }
        if current_sha:
            payload["sha"] = current_sha
            
        try:
            resp = requests.put(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            logging.error(f"State save request failed: {e}")
            time.sleep(2)
            continue
            
        if resp.status_code in (200, 201):
            return True
            
        if resp.status_code == 409:
            logging.info(f"State save conflict (409), retrying {attempt + 1}/3 with 2s backoff...")
            time.sleep(2)
            continue
            
        logging.error(f"State save failed with HTTP {resp.status_code}")
        return False
        
    return False

# ----------------------------------------------------------------------------
# 2. Ticker Mapping (Bulletproof)
# ----------------------------------------------------------------------------
def resolve_ticker(slug: str) -> Optional[str]:
    try:
        sym1 = f"{slug.upper()}.NS"
        t1 = yf.Ticker(sym1)
        if t1.info.get('marketCap'):
            time.sleep(1.1)
            return sym1
    except Exception:
        pass
    time.sleep(1.1)
    
    try:
        search_query = slug.replace('-', ' ')
        search_res = yf.Search(search_query)
        time.sleep(1.1)
        if search_res.quotes:
            return search_res.quotes[0].get('symbol')
    except Exception:
        pass
        
    return None

# ----------------------------------------------------------------------------
# 3. PEAD Module
# ----------------------------------------------------------------------------
def run_pead_module(known_keys: Set[str], new_keys: Set[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    results = []
    warnings = []
    url = "https://www.screener.in/results/latest/"
    
    try:
        # USING CURL_CFFI TO BYPASS CLOUDFLARE
        resp = curl_requests.get(url, timeout=TIMEOUT, impersonate="chrome110")
        time.sleep(1.1)
        resp.raise_for_status()
    except Exception as e:
        warnings.append(f"Screener PEAD fetch failed: {e}")
        return results, warnings
        
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        tbody = soup.find("tbody")
        if not tbody:
            warnings.append("Screener PEAD: No tbody found")
            return results, warnings
            
        rows = tbody.find_all("tr")[:10]
    except Exception as e:
        warnings.append(f"Screener PEAD parse failed: {e}")
        return results, warnings
        
    for row in rows:
        ticker = "UNKNOWN"
        try:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
                
            link = cols[0].find("a")
            if not link:
                continue
                
            href = link.get("href", "")
            match = re.search(r'/company/([^/]+)/', href)
            if not match:
                continue
                
            slug = match.group(1)
            symbol = resolve_ticker(slug)
            
            if not symbol:
                logging.info(f"Skipping {slug}: resolve_ticker failed")
                continue
                
            yf_ticker = yf.Ticker(symbol)
            mcap = yf_ticker.info.get('marketCap')
            time.sleep(1.1)
            
            # ₹1000 Crore Filter
            if not mcap or mcap < 10_000_000_000:
                logging.info(f"Skipping {symbol}: mcap {mcap} < 1000 Cr")
                continue
                
            qis = yf_ticker.quarterly_income_stmt
            time.sleep(1.1)
            
            if qis is None or qis.empty:
                continue
                
            cols_dates = [pd.Timestamp(c) for c in qis.columns]
            if len(cols_dates) < 2:
                continue
                
            latest_date = cols_dates[0]
            prev_col = None
            
            for c in cols_dates:
                if c.month == latest_date.month and c.year == latest_date.year - 1:
                    prev_col = c
                    break
                    
            if not prev_col:
                logging.info(f"Skipping {symbol}: no exact prev_col match")
                continue
                
            net_income_row = None
            total_rev_row = None
            
            for idx in qis.index:
                idx_str = str(idx).lower()
                if "net income" in idx_str or "profit after tax" in idx_str:
                    net_income_row = qis.loc[idx]
                if "total revenue" in idx_str or "operating income" in idx_str:
                    total_rev_row = qis.loc[idx]
                    
            if net_income_row is None or total_rev_row is None:
                continue
                
            curr_profit = float(net_income_row[latest_date])
            prev_profit = float(net_income_row[prev_col])
            curr_sales = float(total_rev_row[latest_date])
            prev_sales = float(total_rev_row[prev_col])
            
            if prev_profit <= 5_00_00_000 or curr_profit <= 5_00_00_000:
                logging.info(f"Skipping {symbol}: low profit base")
                continue
                
            profit_growth = ((curr_profit - prev_profit) / prev_profit) * 100
            sales_growth = ((curr_sales - prev_sales) / prev_sales) * 100 if prev_sales > 0 else 0
            
            if profit_growth > 100 or sales_growth > 100:
                dedup_key = f"PEAD:{symbol}:{latest_date.strftime('%Y-%m')}"
                if dedup_key not in known_keys and dedup_key not in new_keys:
                    new_keys.add(dedup_key)
                    results.append({
                        "category": "PEAD",
                        "ticker": symbol,
                        "sort_value": max(profit_growth, sales_growth),
                        "message": f"📈 PEAD: {symbol}\nProfit Growth: {profit_growth:.1f}% | Sales Growth: {sales_growth:.1f}%"
                    })
        except Exception as e:
            logging.info(f"PEAD ticker {ticker} failed: {e}")
            continue
            
    return results, warnings

# ----------------------------------------------------------------------------
# 4. Big Orders Module
# ----------------------------------------------------------------------------
def run_big_orders_module(known_keys: Set[str], new_keys: Set[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    results = []
    warnings = []
    
    try:
        # USING CURL_CFFI TO BYPASS AKAMAI
        session = curl_requests.Session(impersonate="chrome110")
        session.get("https://www.nseindia.com/", timeout=TIMEOUT)
        time.sleep(1.1)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*"
        }
        resp = session.get("https://www.nseindia.com/api/corporate-announcements?index=equities", headers=headers, timeout=TIMEOUT)
        time.sleep(1.1)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        warnings.append(f"NSE API fetch failed: {e}")
        return results, warnings
        
    pattern = re.compile(r'(?:order|contract|award|work|project)[^.]*?(?:Rs\.?|INR|₹)?\s*(\d+(?:\.\d+)?)\s*(?:crore|cr)\b', re.IGNORECASE)
    
    for item in data:
        try:
            desc = str(item.get("desc", ""))
            symbol = str(item.get("symbol", "")).strip()
            date = str(item.get("an_dt", datetime.now().strftime("%Y-%m-%d")))
            
            if not symbol:
                continue
                
            matches = pattern.findall(desc)
            if not matches:
                continue
                
            valid_values = [float(m) for m in matches if 50 <= float(m) <= 50000]
            if not valid_values:
                continue
                
            order_value = max(valid_values)
            
            ticker = f"{symbol}.NS"
            yf_ticker = yf.Ticker(ticker)
            total_revenue = yf_ticker.info.get('totalRevenue')
            time.sleep(1.1)
            
            if not total_revenue or total_revenue <= 0:
                continue
                
            revenue_cr = total_revenue / 10_000_000
            ratio = (order_value / revenue_cr) * 100
            
            if ratio > 100:
                category = "KAI GUNA"
            elif ratio > 50:
                category = "HIGH IMPACT"
            elif ratio > 20:
                category = "MATERIAL ORDER"
            else:
                continue
                
            dedup_key = f"ORDER:{symbol}:{date}:{order_value}"
            if dedup_key not in known_keys and dedup_key not in new_keys:
                new_keys.add(dedup_key)
                results.append({
                    "category": category,
                    "ticker": ticker,
                    "sort_value": ratio,
                    "message": f"🔥 [{category}] {symbol}: Order ₹{order_value:.0f} Cr vs Rev ₹{revenue_cr:.0f} Cr"
                })
        except Exception as e:
            logging.info(f"Big Order item parse failed: {e}")
            continue
            
    return results, warnings

# ----------------------------------------------------------------------------
# 5. Buyback Module
# ----------------------------------------------------------------------------
def run_buyback_module(known_keys: Set[str], new_keys: Set[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    results = []
    warnings = []
    url = "https://www.screener.in/screens/139/buybacks/"
    
    try:
        # USING CURL_CFFI TO BYPASS CLOUDFLARE
        resp = curl_requests.get(url, timeout=TIMEOUT, impersonate="chrome110")
        time.sleep(1.1)
        resp.raise_for_status()
    except Exception as e:
        warnings.append(f"Screener Buyback fetch failed: {e}")
        return results, warnings
        
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table")
        if not table:
            warnings.append("Screener Buyback: No table found")
            return results, warnings
            
        rows = table.find("tbody").find_all("tr")
    except Exception as e:
        warnings.append(f"Screener Buyback parse failed: {e}")
        return results, warnings
        
    for row in rows:
        ticker = "UNKNOWN"
        try:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
                
            name_link = cols[0].find("a")
            if not name_link:
                continue
                
            href = name_link.get("href", "")
            match = re.search(r'/company/([^/]+)/', href)
            if not match:
                continue
                
            slug = match.group(1)
            symbol = resolve_ticker(slug)
            
            if not symbol:
                continue
                
            buyback_price_str = cols[1].text.strip().replace(",", "").replace("₹", "").replace("Rs", "")
            buyback_price = float(buyback_price_str)
            
            yf_ticker = yf.Ticker(symbol)
            cmp = yf_ticker.info.get("currentPrice")
            time.sleep(1.1)
            
            if not cmp or cmp <= 0:
                continue
                
            premium = ((buyback_price - cmp) / cmp) * 100
            if premium > 8:
                dedup_key = f"BUYBACK:{symbol}:{buyback_price}"
                if dedup_key not in known_keys and dedup_key not in new_keys:
                    new_keys.add(dedup_key)
                    results.append({
                        "category": "BUYBACK",
                        "ticker": symbol,
                        "sort_value": premium,
                        "message": f"💰 [THEORETICAL PREMIUM] {symbol}\nBuyback: ₹{buyback_price:.2f} | CMP: ₹{cmp:.2f} | Premium: {premium:.1f}%"
                    })
        except Exception as e:
            logging.info(f"Buyback ticker {ticker} failed: {e}")
            continue
            
    return results, warnings

# ----------------------------------------------------------------------------
# 6. Delivery Sequence & Output Formatting
# ----------------------------------------------------------------------------
def send_telegram_alert(msg: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        logging.info("Telegram credentials missing.")
        return False
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg}
    
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        time.sleep(1.1)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok") is True:
                return True
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")
        
    return False

def format_and_send_output(pead_res, orders_res, buyback_res, warnings, known_keys, new_keys):
    all_results = pead_res + orders_res + buyback_res
    
    category_priority = {
        "KAI GUNA": 1,
        "BUYBACK": 2,
        "PEAD": 3,
        "HIGH IMPACT": 4,
        "MATERIAL ORDER": 5
    }
    
    all_results.sort(key=lambda x: (
        category_priority.get(x["category"], 99),
        -x["sort_value"],
        x["ticker"]
    ))
    
    if not all_results:
        msg = "✅ Scan Complete: No candidates found today."
        if warnings:
            msg += "\n\n⚠️ WARNINGS:\n" + "\n".join(f"- {w}" for w in warnings)
    else:
        message_lines = ["🚀 DAILY ARTHA MULTIBAGGER LIST\n"]
        for item in all_results:
            message_lines.append(item["message"])
            
        if warnings:
            message_lines.append("\n⚠️ WARNINGS:")
            for w in warnings:
                message_lines.append(f"- {w}")
                
        msg = "\n".join(message_lines)
        
        while len(msg) > 4000 and len(all_results) > 0:
            all_results.pop()
            message_lines = ["🚀 DAILY ARTHA MULTIBAGGER LIST\n"]
            for item in all_results:
                message_lines.append(item["message"])
            if warnings:
                message_lines.append("\n⚠️ WARNINGS:")
                for w in warnings:
                    message_lines.append(f"- {w}")
            msg = "\n".join(message_lines)
            
    success = send_telegram_alert(msg)
    if success and new_keys:
        save_state(new_keys)
    elif not success:
        logging.error("Telegram failed, state NOT saved.")

# ----------------------------------------------------------------------------
# Main Execution
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Project Artha V13.0 Scanner")
    parser.add_argument("--self-test", action="store_true", help="Run offline self-tests and exit")
    args = parser.parse_args()

    if args.self_test:
        sys.argv = [sys.argv[0]]
        unittest.main(module=__name__, exit=False, verbosity=2)
        return

    logging.info("Starting Project Artha V13.0 Scan...")
    
    try:
        known_keys, current_sha = load_state()
    except Exception as e:
        logging.critical(f"State load aborted: {e}")
        return
        
    new_keys: Set[str] = set()
    all_warnings: List[str] = []

    pead_res, pead_warn = run_pead_module(known_keys, new_keys)
    all_warnings.extend(pead_warn)

    orders_res, orders_warn = run_big_orders_module(known_keys, new_keys)
    all_warnings.extend(orders_warn)

    buyback_res, buyback_warn = run_buyback_module(known_keys, new_keys)
    all_warnings.extend(buyback_warn)

    format_and_send_output(pead_res, orders_res, buyback_res, all_warnings, known_keys, new_keys)
    logging.info("Scan completed.")

# ----------------------------------------------------------------------------
# Offline Self-Tests
# ----------------------------------------------------------------------------
class TestProjectArthaV13(unittest.TestCase):
    
    @mock.patch('yfinance.Search')
    @mock.patch('yfinance.Ticker')
    def test_resolve_ticker(self, mock_ticker, mock_search):
        mock_ticker.return_value.info.get.return_value = None
        
        mock_search_instance = mock.Mock()
        mock_search_instance.quotes = [{'symbol': 'TCS.NS'}]
        mock_search.return_value = mock_search_instance
        
        with mock.patch('time.sleep'):
            res = resolve_ticker("tata-consultancy-services")
        self.assertEqual(res, "TCS.NS")

    def test_quarter_matching(self):
        latest_date = pd.Timestamp("2024-12-31")
        cols_dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2024-09-30"), pd.Timestamp("2023-12-31")]
        prev_col = None
        for c in cols_dates:
            if c.month == latest_date.month and c.year == latest_date.year - 1:
                prev_col = c
                break
        self.assertEqual(prev_col, pd.Timestamp("2023-12-31"))

    def test_regex_extraction(self):
        pattern = re.compile(r'(?:order|contract|award|work|project)[^.]*?(?:Rs\.?|INR|₹)?\s*(\d+(?:\.\d+)?)\s*(?:crore|cr)\b', re.IGNORECASE)
        text = "Order 500 Cr, Revenue 100 Cr"
        matches = pattern.findall(text)
        valid_values = [float(m) for m in matches if 50 <= float(m) <= 50000]
        self.assertEqual(max(valid_values), 500.0)

    @mock.patch.object(sys.modules[__name__], 'save_state')
    @mock.patch.object(sys.modules[__name__], 'send_telegram_alert', return_value=False)
    def test_state_not_saved_on_telegram_fail(self, mock_send, mock_save):
        format_and_send_output([], [], [], [], set(), {"NEW_KEY"})
        mock_save.assert_not_called()

if __name__ == "__main__":
    main()
