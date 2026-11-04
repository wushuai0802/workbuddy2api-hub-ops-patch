"""Native v1.10.9 features retain the local protocol and accounting contracts."""
import copy
import json
import os
from pathlib import Path
import time
import types
import unittest
from unittest import mock

from test_account_slot_lifecycle import APIS
from test_integration import BODY, Handler, IntegrationTests, MODEL, REAL_RECORD, turn, wb
from test_v1100 import V1100Tests, REAL_ERROR

C=wb.wb_codearts


class V1109Tests(IntegrationTests):
    def test_codearts_tool_normalization_uses_strict_original_names(self):
        tool={'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':{'q':{'type':'string'}}}}}
        request={'model':'GLM-5.2','messages':[{'role':'user','content':'synthetic'}],
                 'tools':[tool,{'type':'function','function':{'name':' bad '}},
                          {'type':'function','function':{'name':123}},{'type':'web_search'}]}
        original=copy.deepcopy(request)
        body=C.build_chat_body(request)
        self.assertEqual([t['function']['name']for t in body['tools']],['lookup'])
        self.assertEqual(body['tools'][0]['function']['parameters'],tool['function']['parameters'])
        self.assertEqual(request,original)
        request['tools']=request['tools'][1:];request['tool_choice']='required'
        body=C.build_chat_body(request)
        self.assertNotIn('tools',body);self.assertNotIn('tool_choice',body)

    def test_codearts_vision_body_preserves_all_six_owned_response_paths(self):
        image='data:image/png;base64,c3ludGhldGlj'
        request={'model':'GLM-5.2','messages':[{'role':'user','content':[
            {'type':'text','text':'describe'},{'type':'image_url','image_url':{'url':image}}]}]}
        original=copy.deepcopy(request)
        body=C.build_chat_body(request)
        self.assertEqual(body['messages'][0]['content'][1]['image_url']['url'],image)
        self.assertEqual(request,original)
        for api in APIS:
            with self.subTest(api=api):
                self.records.clear()
                source,account,pool=V1100Tests.source(self,turn({'content':'synthetic answer'},tokens=41))
                with mock.patch.object(wb,'open_upstream')as reopened:
                    _,output=V1100Tests.invoke(self,api,source,account,body=body)
                self.assertIn('synthetic answer',output)
                pool.release.assert_called_once_with(account);reopened.assert_not_called()
                self.assertEqual(len(self.records),1)
                kind,args,fields=self.records[0]
                self.assertEqual(kind,'usage');self.assertEqual(args[1]['total_tokens'],41)
                self.assertEqual(fields['realm'],'codearts')

    def test_codearts_open_rejections_keep_429_and_single_provider_error_row(self):
        for api in ('chat','responses','messages'):
            with self.subTest(api=api):
                self.records.clear();h=Handler();h.key_entry={'id':'synthetic'}
                for name in ('_codearts_error','_codearts_anthropic_error'):
                    setattr(h,name,types.MethodType(getattr(wb.Handler,name),h))
                h._open_codearts_chat=mock.Mock(side_effect=C.CodeArtsError('synthetic limited',status=429))
                if api=='chat':
                    wb.Handler._handle_codearts_chat(h,dict(BODY),'session','codearts')
                elif api=='responses':
                    wb.Handler._handle_codearts_responses(h,dict(BODY),'session',MODEL,False,False,set(),{}, {},{},time.time())
                else:
                    wb.Handler._handle_codearts_messages(h,dict(BODY),'session',MODEL,False,{},time.time())
                self.assertEqual(h.json_body['error']['type'],'rate_limit_error')
                self.assertEqual(len(self.records),1)
                self.assertEqual(self.records[0][0],'error')
                self.assertEqual(self.records[0][1][1],429)
                self.assertEqual(self.records[0][2]['realm'],'codearts')

    def test_nested_namespace_choice_survives_codearts_body_conversion(self):
        request={'model':'GLM-5.2','input':'synthetic',
                 'tools':[{'type':'namespace','name':'outer','tools':[
                     {'type':'namespace','name':'inner','tools':[
                         {'type':'function','name':'lookup','parameters':{'type':'object','properties':{}}}]}]}],
                 'tool_choice':{'type':'function','function':{'name':'outer::inner::lookup'}}}
        chat=wb.responses_to_chat(request)
        self.assertEqual(chat['_namespace_map']['lookup'],'outer__inner')
        self.assertEqual(chat['tool_choice']['function']['name'],'lookup')
        body=C.build_chat_body(chat)
        self.assertEqual(body['tool_choice'],'required')
        self.assertEqual([t['function']['name']for t in body['tools']],['lookup'])

    def test_native_rotation_fold_counts_local_success_and_error_rows_once(self):
        with mock.patch.dict(wb._daily_usage,{'day':'','totals':None,'credits':None,'models':None,
                    'offset':0,'at':0.0,'key':None,'tail':None},clear=True):
            REAL_RECORD(MODEL,{'total_tokens':100,'credit':1},account='old',realm='codearts')
            self.assertEqual(wb.daily_usage_stats(ttl=0)['tokens']['old'],100)
            old_offset=wb._daily_usage['offset'];os.truncate(wb.USAGE_LOG,0)
            for _ in range(4):
                REAL_RECORD(MODEL,{'total_tokens':17,'credit':2},account='new',realm='codearts')
                REAL_ERROR(MODEL,502,'synthetic read failure',usage={'total_tokens':19,'credit':3},
                           account='new',realm='codearts')
            self.assertGreater(Path(wb.USAGE_LOG).stat().st_size,old_offset)
            stats=wb.daily_usage_stats(ttl=0)
            self.assertNotIn('old',stats['tokens'])
            self.assertEqual(stats['tokens']['new'],144)
            self.assertEqual(stats['credits']['new'],20)
            rows=[json.loads(line)for line in Path(wb.USAGE_LOG).read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(rows),8)
            self.assertEqual({r['realm']for r in rows},{'codearts'})


if __name__=='__main__':
    suite=unittest.TestSuite(V1109Tests(n)for n in V1109Tests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
