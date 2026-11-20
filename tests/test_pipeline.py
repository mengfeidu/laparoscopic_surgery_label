"""Offline tests only. All videos/credentials/model responses here are test fixtures."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from laparoscopy_5hz.client import AnnotationClient
from laparoscopy_5hz.common import AnnotationError
from laparoscopy_5hz.schema import load_ontology, parse_response, resource, validate_result
from laparoscopy_5hz.security import Redactor, safe_base_url
from laparoscopy_5hz.video import make_cores, sample_source_frames
from laparoscopy_5hz.cost import append_metadata, export_usage
from laparoscopy_5hz.images import image_config, image_manifest, contact_sheet
from PIL import Image
import io

CORE = {"start_frame": 5, "end_frame": 20, "start_seconds": .2, "end_seconds": .8}
FRAMES = [0, 5, 10, 15, 20]
FAKE_CREDENTIAL = "TEST_ONLY_NOT_A_REAL_CREDENTIAL"


def empty_result(core=CORE):
    return {
        "reviewed_start_frame": core["start_frame"],
        "reviewed_end_frame": core["end_frame"],
        "scene_summary_zh": "离线测试样例，不代表真实手术判读。",
        "events": [], "uncertain_ranges": [], "key_events": [],
    }


def example_event():
    # Artificial schema fixture, never used as a medical annotation.
    return {
        "start_frame": 5, "end_frame": 10, "instrument_code": "grasper",
        "tool_position": "left", "tool_description_zh": "测试器械",
        "action_code": "grasp", "target_code": "unknown_tissue",
        "target_detail_zh": "测试目标", "target_certainty": "uncertain", "mediator": "none",
        "result_zh": "未见明确结果", "evidence_zh": "5帧为格式测试占位，不是手术证据。",
        "confidence": .5,
    }


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.ontology = load_ontology()

    def test_empty_lists_and_half_open_core(self):
        validate_result(empty_result(), CORE, self.ontology, FRAMES)
        result = empty_result()
        result["key_events"] = [{"frame": 20, "kind": "other", "description_zh": "测试"}]
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)

    def test_reject_unsampled_boundaries_and_out_of_core(self):
        result = empty_result()
        result["events"] = [example_event()]
        validate_result(result, CORE, self.ontology, FRAMES)
        result["events"][0]["start_frame"] = 6
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)
        result["events"][0]["start_frame"] = 0
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)

    def test_reject_wrong_reviewed_core_unknown_code_duplicate_event(self):
        result = empty_result()
        result["reviewed_start_frame"] = 0
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)
        result = empty_result()
        result["events"] = [example_event()]
        result["events"][0]["action_code"] = "camera_move"
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)
        result["events"] = [example_event(), example_event()]
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)

    def test_parallel_different_instruments_are_preserved(self):
        result = empty_result()
        other = example_event()
        other.update(instrument_code="dissector", tool_position="right", action_code="blunt_dissect")
        result["events"] = [example_event(), other]
        self.assertEqual(len(validate_result(result, CORE, self.ontology, FRAMES)["events"]), 2)

    def test_gauze_target_and_mediator_are_separate(self):
        result = empty_result()
        event = example_event()
        event.update(action_code="compress", target_code="gauze", mediator="none")
        result["events"] = [event]
        validate_result(result, CORE, self.ontology, FRAMES)
        event["mediator"] = "gauze"
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)
        event.update(action_code="wipe", target_code="unknown_tissue", mediator="gauze")
        validate_result(result, CORE, self.ontology, FRAMES)

    def test_strict_json_no_fences_duplicate_keys_nan(self):
        for text in ('```json\n{}\n```', '{"x":1,"x":2}', '{"x":NaN}', '{} trailing'):
            with self.assertRaises(AnnotationError):
                parse_response(text)

    def test_newline_repair_is_explicit(self):
        result, meta = parse_response('{"reviewed_end_\nframe":20}', with_metadata=True)
        self.assertEqual(result, {"reviewed_end_frame":20})
        self.assertTrue(meta["newline_repair_applied"])
        self.assertFalse(parse_response('{\n"x":1\n}', with_metadata=True)[1]["newline_repair_applied"])
        for text in ('{"x":1,\n"x":2}', '{"x":\nNaN}', '```\n{}\n```'):
            with self.assertRaises(AnnotationError):
                parse_response(text)

    def test_end_may_be_last_supported_frame_plus_one(self):
        result = empty_result()
        event = example_event()
        event["end_frame"] = 16
        result["events"] = [event]
        validate_result(result, CORE, self.ontology, FRAMES)
        event["end_frame"] = 17
        with self.assertRaises(AnnotationError):
            validate_result(result, CORE, self.ontology, FRAMES)

    def test_custom_ontology_overrides_allowed_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"ontology.json"
            path.write_text('{"action_code":["custom_action"]}')
            ontology = load_ontology(path)
            event = example_event()
            event["action_code"] = "custom_action"
            result = empty_result()
            result["events"] = [event]
            validate_result(result, CORE, ontology, FRAMES)


class FrameTests(unittest.TestCase):
    def test_5hz_uses_original_global_frames(self):
        times = [n/25 for n in range(50)]
        timeline = {"times": times, "duration_seconds": 2, "source_frame_count": 50}
        self.assertEqual(sample_source_frames(times, 2), list(range(0, 50, 5)))
        cores = make_cores(timeline, .4, 1.2, .4)
        self.assertEqual([(c["start_frame"],c["end_frame"]) for c in cores], [(10,20),(20,30)])

    def test_vfr_uses_actual_pts_never_duplicate(self):
        times = [0,.04,.08,.12,.16,.21,.25,.29,.33,.37,.41]
        self.assertEqual(sample_source_frames(times,.45),[0,5,10])


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint,kwargs))
        return next(self.responses)


class ClientTests(unittest.TestCase):
    def test_invalid_response_cost_is_included(self):
        responses = [FakeResponse(200, {"usage":{"prompt_tokens":100,"completion_tokens":20},
            "choices":[{"message":{"content":text}}]})
            for text in ("invalid", json.dumps(empty_result()))]
        client = AnnotationClient("http://localhost/v1", FAKE_CREDENTIAL, Redactor(),
                                  session=FakeSession(responses), sleeper=lambda _:None)
        _, meta = client.annotate({"model":"fixture","messages":[]}, CORE,load_ontology(),FRAMES)
        self.assertEqual(meta["usage"]["prompt_tokens"],200)
        self.assertEqual(meta["usage"]["completion_tokens"],40)
        self.assertTrue(meta["usage"]["usage_complete"])

    def test_failed_calls_have_auditable_usage(self):
        session = FakeSession([FakeResponse(200,{"usage":{"prompt_tokens":100,"completion_tokens":20},
            "choices":[{"message":{"content":"invalid"}}]})])
        client = AnnotationClient("http://localhost/v1", FAKE_CREDENTIAL, Redactor(),attempts=1,
                                  session=session, sleeper=lambda _:None)
        with self.assertRaises(AnnotationError):
            client.annotate({"model":"fixture","messages":[]},CORE,load_ontology(),FRAMES)
        self.assertEqual(client.last_metadata["attempt_records"][0]["usage"]["prompt_tokens"],100)

    def test_auth_error_no_retry_no_key_in_exception(self):
        session = FakeSession([FakeResponse(401, {"error":FAKE_CREDENTIAL})])
        client = AnnotationClient("http://localhost/v1", FAKE_CREDENTIAL, Redactor([FAKE_CREDENTIAL]),
                                  session=session, sleeper=lambda _:None)
        with self.assertRaises(AnnotationError) as error:
            client.annotate({"model":"gemini-4-argon","messages":[]},CORE,load_ontology(),FRAMES)
        self.assertNotIn(FAKE_CREDENTIAL,str(error.exception))
        self.assertEqual(len(session.calls),1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_invalid_core_corrected_on_retry(self):
        invalid = empty_result()
        invalid["reviewed_start_frame"] = 0
        responses = [FakeResponse(200,{"choices":[{"message":{"content":json.dumps(x)}}]})
                     for x in (invalid,empty_result())]
        session = FakeSession(responses)
        client = AnnotationClient("http://localhost/v1",FAKE_CREDENTIAL,Redactor([FAKE_CREDENTIAL]),
                                  session=session,sleeper=lambda _:None)
        result,meta = client.annotate({"model":"gemini-4-argon","messages":[]},CORE,load_ontology(),FRAMES)
        self.assertEqual(result["reviewed_start_frame"],5)
        self.assertEqual(meta["attempts"],2)
        self.assertNotIn(FAKE_CREDENTIAL,json.dumps(meta))

    def test_refusal_no_retry(self):
        session = FakeSession([FakeResponse(200,{"choices":[{"message":{"refusal":"blocked"}}]})])
        client = AnnotationClient("http://localhost/v1",FAKE_CREDENTIAL,Redactor([FAKE_CREDENTIAL]),
                                  session=session,sleeper=lambda _:None)
        with self.assertRaises(AnnotationError):
            client.annotate({"model":"gemini-4-argon","messages":[]},CORE,load_ontology(),FRAMES)
        self.assertEqual(len(session.calls),1)

    def test_redaction_and_url_credentials(self):
        redactor = Redactor([" "+FAKE_CREDENTIAL+" "])
        self.assertNotIn(FAKE_CREDENTIAL,redactor.text(FAKE_CREDENTIAL))
        for url in ("https://user:password@example.test/v1","https://example.test/v1?key=secret",
                    "http://remote.example.test/v1"):
            with self.assertRaises(AnnotationError):
                safe_base_url(url,redactor)


class OfflineIntegrationTests(unittest.TestCase):
    def test_cli_frames_request_resume_and_config_guard(self):
        requests_received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests_received.append(body)
                params=json.loads(body["messages"][1]["content"][0]["text"].split("\n",1)[1])
                core={"start_frame":params["core"][0],"end_frame":params["core"][1]}
                response={"model":"offline_fixture","usage":{"prompt_tokens":100,"completion_tokens":20},
                    "choices":[{"finish_reason":"stop",
                    "message":{"content":json.dumps(empty_result(core),ensure_ascii=False)}}]}
                content=json.dumps(response,ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="laparoscopy-offline-test-") as tmp:
                tmp=Path(tmp)
                video=tmp/"test_fixture.mp4"
                subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i",
                    "testsrc2=size=160x128:rate=25:duration=1.2","-c:v","libx264","-pix_fmt",
                    "yuv420p",str(video)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                env=dict(os.environ)
                for key in ("XHUB_API_KEY","OPENAI_API_KEY","ANNOTATION_API_KEY",
                            "ANNOTATION_BASE_URL","XHUB_BASE_URL","ANNOTATION_MODEL"):
                    env.pop(key,None)
                env["ANNOTATION_API_KEY"]=FAKE_CREDENTIAL
                env["PYTHONPATH"]=str(Path(__file__).resolve().parents[1])
                output=tmp/"output"
                cmd=[sys.executable,"-m","laparoscopy_5hz","annotate","--video",str(video),
                     "--output",str(output),"--base-url",f"http://127.0.0.1:{server.server_port}/v1",
                     "--window-seconds","0.6","--context-seconds","0","--width","160"]
                cmd += ["--image-layout","individual"]
                def run(extra=()):
                    return subprocess.run(cmd+list(extra),cwd=tmp,env=env,text=True,capture_output=True)
                dry=run(["--dry-run"])
                self.assertEqual(dry.returncode,0,dry.stderr)
                self.assertEqual(requests_received,[])
                manifest=json.loads((output/"sampled_frames.json").read_text())
                self.assertEqual([f["frame"] for f in manifest["frames"]],[0,5,10,15,20,25])
                first=run()
                self.assertEqual(first.returncode,0,first.stderr)
                self.assertEqual(len(requests_received),2)
                for payload in requests_received:
                    self.assertEqual(payload["model"],"gemini-4-argon")
                    self.assertEqual(payload["messages"][0]["content"],resource("prompt_zh.txt"))
                    content=payload["messages"][1]["content"]
                    self.assertEqual(sum(x["type"]=="image_url" for x in content),3)
                second=run()
                self.assertEqual(second.returncode,0,second.stderr)
                self.assertEqual(len(requests_received),2)
                self.assertIn("复用 2",second.stdout)
                usage=json.loads((output/"usage.json").read_text())
                self.assertEqual(usage["requests"],2)
                self.assertEqual(usage["prompt_tokens"],200)
                self.assertEqual(usage["completion_tokens"],40)
                self.assertEqual(usage["unknown_usage_requests"],0)
                self.assertTrue(usage["usage_complete"])
                custom=tmp/"custom.txt"
                custom.write_text("离线自定义知识库")
                grid_cmd=cmd.copy()
                grid_cmd[grid_cmd.index(str(output))]=str(tmp/"grid_output")
                grid_cmd[grid_cmd.index("individual")]="grid"
                grid_cmd += ["--prompt",str(custom)]
                grid_run=subprocess.run(grid_cmd,cwd=tmp,env=env,text=True,capture_output=True)
                self.assertEqual(grid_run.returncode,0,grid_run.stderr)
                self.assertEqual(requests_received[-1]["messages"][0]["content"],custom.read_text())
                content=requests_received[-1]["messages"][1]["content"]
                self.assertEqual(sum(x["type"]=="image_url" for x in content),1)
                mismatch=run(["--model","different-model"])
                self.assertNotEqual(mismatch.returncode,0)
                self.assertEqual(len(requests_received),4)
                for file in output.rglob("*"):
                    if file.is_file() and file.suffix in {".json",".csv"}:
                        self.assertNotIn(FAKE_CREDENTIAL,file.read_text(encoding="utf-8-sig"))
                self.assertNotIn(FAKE_CREDENTIAL,first.stdout+first.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class DeliveryCostTests(unittest.TestCase):
    def test_grid_preserves_frames_and_labels(self):
        config = image_config("grid", "high", 4, 2, 160, 128)
        self.assertEqual(image_manifest([0,5,10,15,20], config)["image_groups"],
                         [[0,5,10,15],[20]])
        with tempfile.TemporaryDirectory() as tmp:
            for n in [0,5,10,15]:
                Image.new("RGB",(160,156),"red").save(Path(tmp)/f"{n:09d}.jpg")
            data=contact_sheet([0,5,10,15],{"times":[n/25 for n in range(20)]},tmp,config)
            self.assertEqual(Image.open(io.BytesIO(data)).size, (320,304))

    def test_usage_resume_and_failure_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            path=output/"call_metadata"/"window_00000.json"
            append_metadata(path, {"attempt_records":[{"usage":{"prompt_tokens":100,"completion_tokens":10}}]})
            append_metadata(path, {"attempt_records":[{"usage":{}},
                                                      {"usage":{"prompt_tokens":200,"completion_tokens":20}}]})
            first=export_usage(output,2,10)
            second=export_usage(output,2,10)
            self.assertEqual(first,second)
            self.assertEqual(first["prompt_tokens"],300)
            self.assertEqual(first["requests"],3)
            self.assertEqual(first["unknown_usage_requests"],1)
            self.assertAlmostEqual(first["estimated_usd"],.0009)

    def test_missing_historical_usage_is_not_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            (output/"windows").mkdir()
            (output/"windows/window_00000.json").write_text("{}")
            report=export_usage(output)
            self.assertEqual(report["windows_without_usage"],1)
            self.assertFalse(report["usage_complete"])

    def test_legacy_metadata_retains_unknown_prior_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            path=output/"call_metadata/window_00000.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"usage":{"prompt_tokens":100,"completion_tokens":10},
                                        "attempts":3,"prior_attempts":[{"category":"invalid_output"}]}))
            report=export_usage(output,2,10)
            self.assertEqual(report["prompt_tokens"],100)
            self.assertEqual(report["requests"],3)
            self.assertEqual(report["unknown_usage_requests"],2)
            self.assertTrue(report["estimated_usd_is_lower_bound"])


if __name__ == "__main__":
    unittest.main()
