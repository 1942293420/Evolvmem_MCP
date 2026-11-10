"""Synthetic-only checks for the acknowledged-prefix hashing helper."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import zstandard
except ImportError:
    raise unittest.SkipTest("Install zstandard to test compressed prefix sources")
import importlib.util
spec=importlib.util.spec_from_file_location('prefix_hashes',Path(__file__).parents[2]/'scripts/windows/prefix_hashes.py')
app=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=app
spec.loader.exec_module(app)


class PrefixHashesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.raw=(bytes(range(256))+b'\n')*2050
        self.path=self.root/'session.jsonl';self.path.write_bytes(self.raw)
    def tearDown(self):self.tmp.cleanup()
    def request(self,**overrides):
        data=dict(path=str(self.path),total_bytes=len(self.raw),
                  sha256=hashlib.sha256(self.raw).hexdigest(),lengths=[0,1,55,56,63,64,65,119,120,127,128,len(self.raw)])
        data.update(overrides);return data
    def run_helper(self,data):
        out=io.StringIO();code=app.run(io.StringIO(json.dumps(data)),out)
        return code,json.loads(out.getvalue())
    def assert_success(self,data,raw):
        code,value=self.run_helper(data);self.assertEqual(code,0,value)
        self.assertEqual(value['sha256'],hashlib.sha256(raw[:data['total_bytes']]).hexdigest())
        self.assertEqual(value['total_bytes'],data['total_bytes'])
        self.assertEqual(value['prefixes'],{str(n):hashlib.sha256(raw[:n]).hexdigest()for n in sorted(set(data['lengths']))})
    def assert_error(self,data,expected=None):
        code,value=self.run_helper(data);self.assertNotEqual(code,0)
        self.assertEqual(set(value),{'error'})
        if expected:self.assertEqual(value['error'],expected)
        self.assertNotIn(str(self.path),json.dumps(value))
        self.assertNotIn('prefixes',value)
    def test_raw_multiple_hash_padding_boundaries(self):self.assert_success(self.request(),self.raw)
    def test_zstd_boundaries_and_concatenated_frames(self):
        p=self.root/'session.jsonl.zst';p.write_bytes(zstandard.ZstdCompressor().compress(self.raw[:100])+zstandard.ZstdCompressor().compress(self.raw[100:]))
        self.assert_success(self.request(path=str(p)),self.raw)
    def test_empty_lengths_still_validate_full_hash(self):
        self.assert_success(self.request(lengths=[]),self.raw)
        self.assert_error(self.request(lengths=[],sha256='0'*64),'sha256_mismatch')
    def test_wrong_sha_never_emits_partial_prefixes(self):self.assert_error(self.request(sha256='0'*64),'sha256_mismatch')
    def test_short_raw(self):self.assert_error(self.request(total_bytes=len(self.raw)+1),'source_too_short')
    def test_truncated_zstd_before_ack_prefix(self):
        p=self.root/'short.zst';compressed=zstandard.ZstdCompressor().compress(self.raw);p.write_bytes(compressed[:len(compressed)//2])
        self.assert_error(self.request(path=str(p)))
    def test_malformed_zstd(self):
        p=self.root/'bad.zst';p.write_bytes(b'not zstd')
        self.assert_error(self.request(path=str(p)),'invalid_compressed_source')
    def test_raw_growing_tail_accepted(self):
        data=self.request();self.path.write_bytes(self.raw+b'new not-acknowledged tail')
        self.assert_success(data,self.raw)
    def test_zstd_growing_or_incomplete_tail_after_ack_prefix_is_ignored(self):
        p=self.root/'growing.zst';p.write_bytes(zstandard.ZstdCompressor().compress(self.raw)+b'partial later frame')
        self.assert_success(self.request(path=str(p)),self.raw)
    def test_prefix_inside_compressed_frame_does_not_require_full_tail(self):
        p=self.root/'tail.zst';p.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(self.raw)[:-4])
        n=200;self.assert_success(self.request(path=str(p),total_bytes=n,sha256=hashlib.sha256(self.raw[:n]).hexdigest(),lengths=[0,64,n]),self.raw)
    def test_source_rewrite_refused(self):
        data=self.request();self.path.write_bytes(b'changed'+self.raw[7:]);self.assert_error(data,'sha256_mismatch')
    def test_duplicate_unsorted_lengths_are_deduplicated(self):self.assert_success(self.request(lengths=[128,0,64,128,64,1]),self.raw)
    def test_bounds_and_strict_types(self):
        for lengths in [[-1],[len(self.raw)+1],[True],[1.0],['1'],None,{}]:
            with self.subTest(lengths=lengths):self.assert_error(self.request(lengths=lengths),'invalid_input')
        for total in [-1,2**31,True,1.0,'1',None]:
            with self.subTest(total=total):self.assert_error(self.request(total_bytes=total),'invalid_input')
    def test_hash_format_strict(self):
        for sha in ['0'*63,'g'*64,'0'*64+'\n','',None,1]:
            with self.subTest(sha=sha):self.assert_error(self.request(sha256=sha),'invalid_input')
        self.assert_success(self.request(sha256=hashlib.sha256(self.raw).hexdigest().upper()),self.raw)
    def test_length_count_limit_applies_before_deduplication(self):
        self.assert_error(self.request(lengths=[0]*10001),'invalid_input')
        self.assert_success(self.request(lengths=[0]*10000),self.raw)
    def test_missing_and_non_file_source(self):
        self.assert_error(self.request(path=str(self.root/'missing')),'source_unavailable')
        self.assert_error(self.request(path=str(self.root)),'source_unavailable')
        self.assert_error(self.request(path=4),'invalid_input')
    def test_empty_ack_prefix(self):
        self.assert_success(self.request(total_bytes=0,sha256=hashlib.sha256(b'').hexdigest(),lengths=[0]),b'')
    def test_single_forward_read_and_bounded_chunks(self):
        class CountReader(io.BytesIO):
            def __init__(self,raw):super().__init__(raw);self.reads=[]
            def read(self,size=-1):self.reads.append(size);return super().read(size)
        reader=CountReader(self.raw);lengths=[0,55,56,63,64,65,119,120,127,128,len(self.raw)]
        result=app.hash_prefixes(reader,len(self.raw),hashlib.sha256(self.raw).hexdigest(),lengths)
        self.assertEqual(sum(reader.reads),len(self.raw));self.assertLessEqual(max(reader.reads),app.READ_BYTES)
        self.assertEqual(result['prefixes']['128'],hashlib.sha256(self.raw[:128]).hexdigest())
    def test_bad_json_is_fixed_error_without_traceback(self):
        out=io.StringIO();self.assertNotEqual(app.run(io.StringIO('{bad'),out),0)
        self.assertEqual(json.loads(out.getvalue()),{'error':'invalid_input'})
    def test_cli_success_and_failure_exit(self):
        script=Path(app.__file__)
        for good in [True,False]:
            data=self.request(sha256=hashlib.sha256(self.raw).hexdigest()if good else '0'*64)
            proc=subprocess.run([sys.executable,str(script)],input=json.dumps(data).encode(),capture_output=True)
            self.assertEqual(proc.returncode,0 if good else 1);self.assertEqual(proc.stderr,b'')
            value=json.loads(proc.stdout)
            self.assertEqual('prefixes'in value,good)
    def test_cli_utf8_chinese_path_and_bom(self):
        script=Path(app.__file__);p=self.root/'中文会话.jsonl';p.write_bytes(self.raw)
        payload=json.dumps(self.request(path=str(p)),ensure_ascii=False).encode('utf-8-sig')
        proc=subprocess.run([sys.executable,str(script)],input=payload,capture_output=True)
        self.assertEqual(proc.returncode,0);self.assertEqual(proc.stderr,b'')
        self.assertEqual(json.loads(proc.stdout)['sha256'],hashlib.sha256(self.raw).hexdigest())
    def test_cli_invalid_utf8_has_fixed_error(self):
        proc=subprocess.run([sys.executable,str(Path(app.__file__))],input=b'\xff\xfe',capture_output=True)
        self.assertEqual(proc.returncode,1);self.assertEqual(proc.stderr,b'')
        self.assertEqual(json.loads(proc.stdout),{'error':'invalid_input'})


if __name__=='__main__':unittest.main()
