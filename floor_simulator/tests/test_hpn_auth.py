"""The portal's JavaScript crypto (firmware/data/js/hpnauth.js) against OpenSSL (Node) and Python."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FW = Path(__file__).resolve().parents[2] / "firmware"
sys.path.insert(0, str(FW / "tools"))
import hpn_auth  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = r"""
const fs = require('fs'), nc = require('crypto');
global.location = { origin: 'http://ctl', protocol: 'http:', host: 'ctl' };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.crypto = { getRandomValues: (a) => nc.randomFillSync(a) };
global.btoa = (s) => Buffer.from(s, 'binary').toString('base64');
global.atob = (s) => Buffer.from(s, 'base64').toString('binary');
eval(fs.readFileSync(process.argv[2], 'utf8') + '\nglobal.hpnAuth = hpnAuth;');
const A = hpnAuth, hex = A.toHex, te = new TextEncoder();
const out = { sha: [], hmac: [] };
for (let n = 0; n < 300; n += 7) {
  const d = nc.randomBytes(n);
  out.sha.push([d.toString('hex'), hex(A.sha256(d)), nc.createHash('sha256').update(d).digest('hex')]);
}
for (const kl of [0, 16, 64, 65, 200]) {
  const k = nc.randomBytes(kl), m = nc.randomBytes(100);
  out.hmac.push([hex(A.hmac(k, m)), nc.createHmac('sha256', k).update(m).digest('hex')]);
}
out.pbkdf2 = [hex(A.pbkdf2('pässwörd', A.fromHex('00112233445566778899aabbccddeeff'), 1000)),
              nc.pbkdf2Sync('pässwörd', Buffer.from('00112233445566778899aabbccddeeff', 'hex'), 1000, 32, 'sha256').toString('hex')];
const k = A.deriveKeys('admin', '00112233445566778899aabbccddeeff', 1000);
out.keys = [hex(k.kAuth), hex(k.kEnc)];
out.sealed = A.seal(k.kEnc, 'abcd', '{"secret":"x"}');
out.roundtrip = A.unseal(k.kEnc, 'abcd', out.sealed);
out.query = A.canonicalQuery('?target=fs&sha256=ab&hpn_sig=1&a=2');
console.log(JSON.stringify(out));
"""


def test_js_crypto_matches_openssl_and_python(tmp_path):
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    res = json.loads(subprocess.check_output(["node", str(h), str(FW / "data" / "js" / "hpnauth.js")], text=True))
    for data, js, ossl in res["sha"]:
        assert js == ossl, f"sha256 mismatch for {len(data) // 2} bytes"
    for js, ossl in res["hmac"]:
        assert js == ossl
    assert res["pbkdf2"][0] == res["pbkdf2"][1]
    k_auth, k_enc = hpn_auth.derive_keys("admin", "00112233445566778899aabbccddeeff", 1000)
    assert res["keys"] == [k_auth.hex(), k_enc.hex()]
    assert res["roundtrip"] == '{"secret":"x"}'
    assert hpn_auth.unseal(k_enc, "abcd", res["sealed"]) == b'{"secret":"x"}'       # JS seal -> Python unseal
    py_sealed = hpn_auth.seal(k_enc, "abcd", b"hello")
    assert hpn_auth.unseal(k_enc, "abcd", py_sealed) == b"hello"
    with pytest.raises(ValueError):
        hpn_auth.unseal(k_enc, "abce", py_sealed)                                      # nonce is bound
    assert res["query"] == hpn_auth.canonical_query("target=fs&sha256=ab&hpn_sig=1&a=2") == "a=2&sha256=ab&target=fs"
