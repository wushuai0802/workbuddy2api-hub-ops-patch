"""Public defaults and explicit compatibility choices, without model requests."""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from test_integration import wb


class V176DispatchTests(unittest.TestCase):
    def handler(self, path, payload):
        handler=wb.Handler.__new__(wb.Handler)
        handler.path=path
        handler._is_panel_route=mock.Mock(return_value=False)
        handler._authorized=mock.Mock(return_value=True)
        handler._key_ok=mock.Mock(return_value=True)
        handler._payload_or_error=mock.Mock(return_value=payload)
        handler._error=mock.Mock()
        handler._anthropic_error=mock.Mock()
        handler._dispatch_chat_post=mock.Mock()
        handler._handle_messages=mock.Mock()
        handler._handle_messages_count_tokens=mock.Mock()
        return handler

    def test_messages_default_gate_precedes_payload_and_generation(self):
        for path in ('/v1/messages','/messages','/v1/messages/count_tokens','/messages/count_tokens'):
            handler=self.handler(path,{'model':'deepseek-v4.1-flash'})
            with mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':''}),mock.patch.object(wb,'open_upstream') as opened:
                handler.do_POST()
            self.assertEqual(handler._anthropic_error.call_args.args[0],400)
            handler._payload_or_error.assert_not_called()
            opened.assert_not_called()

    def test_messages_auth_still_precedes_disabled_policy(self):
        handler=self.handler('/v1/messages',{})
        handler._key_ok.return_value=False
        with mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':''}):handler.do_POST()
        self.assertEqual(handler._anthropic_error.call_args.args[0],401)
        handler._payload_or_error.assert_not_called()

    def test_explicit_messages_opt_in_routes_both_generation_and_count(self):
        for path in ('/v1/messages','/messages','/v1/messages/count_tokens','/messages/count_tokens'):
            handler=self.handler(path,{'model':'deepseek-v4.1-flash','max_tokens':8,'messages':[]})
            with mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':'1'}):handler.do_POST()
            handler._anthropic_error.assert_not_called()
            target=handler._handle_messages_count_tokens if path.endswith('/count_tokens') else handler._handle_messages
            target.assert_called_once()

    def test_alias_dispatch_changes_only_model_before_generation(self):
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages'):
            body={'model':'gpt-5.6-terra','messages':[{'role':'user','content':'Synthetic task'}],
                  'max_tokens':8,'stream':True,'tools':[{'type':'function','function':{'name':'inspect'}}]}
            before=copy.deepcopy(body)
            handler=self.handler(path,body)
            with mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':'1'}), \
                 mock.patch.object(wb,'BACKGROUND_MODEL_NAMES',('gpt-5.6-terra',)), \
                 mock.patch.object(wb,'BACKGROUND_MODEL_TARGET','deepseek-v4.1-flash'),mock.patch.object(wb,'log'):
                handler.do_POST()
            self.assertEqual(body,dict(before,model='deepseek-v4.1-flash'))
            (handler._handle_messages if path.endswith('/messages') else handler._dispatch_chat_post).assert_called_once()

    def test_empty_alias_list_preserves_explicit_model_routing(self):
        body={'model':'gpt-5.6-terra','messages':[]}
        handler=self.handler('/v1/chat/completions',body)
        with mock.patch.object(wb,'BACKGROUND_MODEL_NAMES',()):handler.do_POST()
        self.assertEqual(body['model'],'gpt-5.6-terra')
        handler._dispatch_chat_post.assert_called_once()

    def test_fresh_process_parses_compatibility_environment(self):
        env=dict(os.environ,WB_BACKGROUND_MODEL_NAMES='',WB_ENABLE_ANTHROPIC_MESSAGES='1')
        source='import json,os,wb_proxy as w; print(json.dumps([list(w.BACKGROUND_MODEL_NAMES),os.environ["WB_ENABLE_ANTHROPIC_MESSAGES"]]))'
        value=subprocess.check_output([sys.executable,'-B','-c',source],env=env,text=True,timeout=20)
        self.assertEqual(json.loads(value.splitlines()[-1]),[[],'1'])


if __name__=='__main__':unittest.main()
