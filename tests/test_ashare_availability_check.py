"""Real synthetic HTTP/capture/ordinary readers and unchanged non-publishing host."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from decision_kernel.application.market_source_check import check_sources
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute
from test_market_relay_inputs import Response, REGISTRY, REGISTRY_PATH
from test_market_source_check import SECRET_TEXT
import test_market_source_check as original_checks

START, END = '20260701', '20261015'
NOW = datetime(2026,10,16,5,tzinfo=timezone.utc)

class AvailabilityOpener:
    def __init__(self):
        self.calls=[]; self.conflicts=True; self.master_count=2
        self.failure=None; self.empty=None; self.missing_date=False
        self.stall=False; self.fault=None; self.transient=False

    def open(self,request,timeout):
        url=urlsplit(request.full_url);api=url.path.rsplit('/',1)[-1]
        p={k:v[0] for k,v in parse_qs(url.query).items()};self.calls.append((api,p))
        if self.failure == api:
            raise HTTPError(request.full_url,403,SECRET_TEXT,{},BytesIO(b'not authorized'))
        if self.transient and len(self.calls)==1:
            raise HTTPError(request.full_url,504,SECRET_TEXT,{},BytesIO(b'{"error":"upstream_timeout"}'))
        if self.fault=='timeout' and api=='stock_basic':
            raise URLError(TimeoutError(SECRET_TEXT))
        fields=p['fields'].split(',');code=p.get('ts_code')
        begin=datetime.strptime(START,'%Y%m%d').date();finish=datetime.strptime(END,'%Y%m%d').date()
        dates=[begin+timedelta(days=n) for n in range((finish-begin).days+1)]
        sessions=[d.strftime('%Y%m%d') for d in dates if d.weekday()<5]
        venue=p.get('exchange','SSE');prefix,suffix=('600','SH') if venue=='SSE' else ('000','SZ')
        if api=='trade_cal':
            rows=[dict(exchange=venue,cal_date=d.strftime('%Y%m%d'),is_open=int(d.weekday()<5)) for d in dates]
            if self.fault=='calendar':rows.pop(0)
        elif api=='stock_basic':
            rows=[dict(ts_code=f'{prefix}{i:03d}.{suffix}',symbol=f'{prefix}{i:03d}',name=SECRET_TEXT,
                       exchange=venue,list_status='L',curr_type='CNY',list_date='19990101',delist_date=None)
                  for i in range(self.master_count)]
            if self.conflicts:rows[0]['delist_date']='20250101'
            if self.fault=='all-bad':
                for row in rows:row['delist_date']='20250101'
            if self.fault=='unknown':rows[0]['ts_code']='NOT-A-CODE'
        elif api=='ths_index':
            first='880001.TI' if p['type']=='I' else '885001.TI'
            rows=[dict(ts_code=first,name=SECRET_TEXT,count=1,exchange='A',list_date='19990101',type=p['type']),
                  dict(ts_code='880002.TI' if p['type']=='I' else '885002.TI',name=SECRET_TEXT,
                       count=1,exchange='A',list_date='19990101',type=p['type'])]
            if self.conflicts:rows.append({**rows[0],'count':2})
        elif api=='ths_member':
            rows=[dict(ts_code=code,con_code='600001.SH',con_name=SECRET_TEXT)]
        else:
            rows=[dict(ts_code=code,trade_date=d,close='9876.54321',adj_factor='1.23456789') for d in sessions]
            if self.missing_date and api=='daily':rows=[r for r in rows if r['trade_date']!='20260728']
            if self.fault=='price' and api=='daily':rows[-1]['close']='0'
        if self.empty==api:rows=[]
        offset=0 if self.stall and api=='stock_basic' else int(p.get('offset',0))
        rows=rows[offset:offset+int(p['limit'])]
        return Response(request.full_url,{'code':0,'api_name':api,'count':len(rows),
            'data':{'fields':fields,'items':[[r[f] for f in fields] for r in rows]}})

class AvailabilitySourceCheckTests(unittest.TestCase):
    def run_check(self,opener=None,**extra):
        self.opener=opener or AvailabilityOpener();self.waits=[];self.receipts=[]
        client=RelayHTTP(opener=self.opener,credential='synthetic-key-only')
        return check_sources(**{'registry':REGISTRY,'exchange':'SSE','start':START,'end':END,
            'access_confirmed':True,'selection':'availability','now':NOW,'client':client,
            'sleep':self.waits.append,'progress':self.receipts.append,'clock':lambda:NOW.isoformat(),**extra})

    def test_quarantined_first_codes_do_not_block_valid_three_surface_samples(self):
        out=self.run_check()
        self.assertEqual(out['status'],'available-with-quarantine');self.assertTrue(out['sample_readable'])
        self.assertFalse(out['strict_reference_passed']);self.assertEqual(out['http_attempts'],10)
        for kind in ('stock','sector','concept'):
            view=out['surfaces'][kind]
            self.assertEqual(view['scope_count'],2);self.assertEqual(view['observed_count'],1)
            self.assertEqual(view['reference_quality']['quarantined_count'],1)
            self.assertTrue(all(v=='observed' for w in view['sample_windows'].values() for v in w.values()))
        selected={api:p.get('ts_code') for api,p in self.opener.calls if api in ('daily','adj_factor')}
        self.assertEqual(selected,{'daily':'600001.SH','adj_factor':'600001.SH'})
        for api,p in self.opener.calls:
            if api=='ths_daily':self.assertIn(p['ts_code'],('880002.TI','885002.TI'))
        for receipt in self.receipts+[out]:
            text=json.dumps(receipt)
            for banned in (SECRET_TEXT,'600001.SH','880002.TI','9876.54321','1.23456789','synthetic-key-only',
                           'wire_sha256','query_key','parameters','captured_at'):
                self.assertNotIn(banned,text)

    def test_clean_samples_and_unchanged_strict_mode(self):
        opener=AvailabilityOpener();opener.conflicts=False
        out=self.run_check(opener)
        self.assertEqual(out['status'],'checked-availability-sample');self.assertTrue(out['strict_reference_passed'])
        strict=self.run_check(selection='sample')
        self.assertEqual(strict['status'],'completed-with-gaps');self.assertNotIn('reference_policy',strict)
        self.assertEqual(strict['queries']['master']['reason_code'],'MARKET_RELAY_MASTER_DELIST_DATE')
        self.assertFalse(any(a=='daily' for a,p in self.opener.calls))

    def test_shenzhen_stock_does_not_repeat_ths_queries(self):
        out=self.run_check(selection='stock-availability',exchange='SZSE')
        self.assertEqual(out['status'],'available-with-quarantine');self.assertEqual(out['http_attempts'],4)
        self.assertEqual(set(out['surfaces']),{'stock'})
        self.assertEqual(next(p['ts_code'] for a,p in self.opener.calls if a=='daily'),'000001.SZ')

    def test_small_pages_all_rows_reconciled_not_partial_qualification(self):
        opener=AvailabilityOpener();opener.master_count=502
        out=self.run_check(opener,selection='stock-availability')
        self.assertTrue(out['sample_readable']);master=out['queries']['master']
        self.assertEqual(master['row_count'],502);self.assertEqual(master['page_count'],2)
        self.assertEqual(master['reference_quality']['eligible_count'],501)
        self.assertEqual(master['page_policy'],{'page_size':500,'max_pages':20,'row_ceiling':10000})
        self.assertEqual([int(p.get('offset',0)) for a,p in self.opener.calls if a=='stock_basic'],[0,500])

    def test_stalled_pages_fail_closed_and_cannot_drive_prices(self):
        opener=AvailabilityOpener();opener.master_count=502;opener.stall=True
        out=self.run_check(opener,selection='stock-availability')
        self.assertFalse(out['sample_readable']);self.assertIsNone(out['surfaces']['stock']['scope_count'])
        self.assertEqual(out['queries']['master']['reason_code'],'SOURCE_PAGINATION_STALLED')
        self.assertFalse(any(a=='daily' for a,p in self.opener.calls))

    def test_unavailable_shared_calendar_unknown_not_zero(self):
        opener=AvailabilityOpener();opener.fault='calendar'
        out=self.run_check(opener)
        self.assertFalse(out['sample_readable'])
        self.assertTrue(all(s['scope_count'] is None and s['sample_windows'] is None for s in out['surfaces'].values()))
        self.assertFalse(any(a in ('daily','adj_factor','ths_daily') for a,p in self.opener.calls))

    def test_no_eligible_or_empty_master_not_a_watchlist_fallback(self):
        for field,val in (('fault','all-bad'),('empty','stock_basic'),('fault','unknown')):
            opener=AvailabilityOpener();setattr(opener,field,val)
            with self.subTest(field=field,val=val):
                out=self.run_check(opener,selection='stock-availability')
                self.assertFalse(out['sample_readable']);self.assertFalse(out['surfaces']['stock']['sample_selected'])
                self.assertFalse(any(a=='daily' for a,p in self.opener.calls))

    def test_end_price_or_factor_gaps_never_pass_sample(self):
        for api in ('daily','adj_factor'):
            opener=AvailabilityOpener();opener.empty=api
            with self.subTest(api=api):
                out=self.run_check(opener,selection='stock-availability')
                self.assertFalse(out['sample_readable']);self.assertEqual(out['surfaces']['stock']['scope_count'],2)

    def test_long_window_gap_does_not_erase_usable_short_windows(self):
        opener=AvailabilityOpener();opener.missing_date=True
        out=self.run_check(opener,selection='stock-availability')
        self.assertFalse(out['sample_readable'])
        for convention in ('raw','qfq'):
            windows=out['surfaces']['stock']['sample_windows'][convention]
            self.assertEqual(windows,{'5':'observed','20':'observed','60':'missing-market-sessions'})

    def test_old_retry_and_refusal_controls_remain(self):
        opener=AvailabilityOpener();opener.transient=True
        out=self.run_check(opener,selection='stock-availability')
        self.assertTrue(out['sample_readable']);self.assertEqual(self.waits,[30])
        opener=AvailabilityOpener();opener.failure='stock_basic'
        out=self.run_check(opener)
        self.assertTrue(out['stopped']);self.assertEqual(out['http_attempts'],2)
        self.assertFalse(out['sample_readable']);self.assertEqual(self.waits,[])

    def test_transport_exhaustion_leaves_unknown_and_does_not_retry_other_mode(self):
        opener=AvailabilityOpener();opener.fault='timeout'
        out=self.run_check(opener,selection='stock-availability')
        self.assertEqual(out['http_attempts'],5);self.assertEqual(self.waits,[30,30,30])
        self.assertEqual(out['queries']['master']['row_count'],0)
        self.assertIsNone(out['surfaces']['stock']['scope_count']);self.assertFalse(out['sample_readable'])

    def test_same_manual_host_only_exports_allowlisted_result(self):
        with tempfile.TemporaryDirectory() as d:
            event,env=original_checks.MarketCheckHostTests().context(d)
            event['inputs'].update(start=START,end=END,selection='availability')
            opener=AvailabilityOpener();client=RelayHTTP(opener=opener,credential='synthetic-key-only')
            def checker(**kw):return check_sources(**kw,client=client,clock=lambda:NOW.isoformat())
            out=Path(d)/'receipt'
            self.assertEqual(execute(event,env,out,now=NOW,checker=checker,registry_path=REGISTRY_PATH),0)
            self.assertEqual([p.name for p in out.iterdir()],['result.json'])
            receipt=json.loads((out/'result.json').read_text())
            self.assertEqual(receipt['status'],'available-with-quarantine')
            self.assertFalse(receipt['supplier_data_retained']);self.assertFalse(receipt['source_refs_created'])

if __name__=='__main__':unittest.main()
