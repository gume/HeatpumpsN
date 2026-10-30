'use strict';
/* HPN1 challenge-response authentication for the controller portal.
 *
 * Plain HTTP pages get no crypto.subtle, so SHA-256, HMAC and PBKDF2 are
 * implemented here (validated against OpenSSL and Python hashlib by
 * floor_simulator/tests/test_hpn_auth.py). The protocol is documented in
 * firmware/docs/security.md.
 *
 *   keys     master = PBKDF2-HMAC-SHA256(password, salt, iter, 32)
 *            kAuth = HMAC(master, "hpn-auth"), kEnc = HMAC(master, "hpn-enc")
 *   request  X-HPN-Auth: user=<u>,nonce=<device nonce>,sig=<hex>
 *            sig = HMAC(kAuth, "HPN1\n" METHOD "\n" path "\n" query "\n" nonce "\n" sha256(body))
 *   sealed   {"sealed": base64(iv16 | ct | tag32)}, keystream HMAC(ks, iv|ctr), tag over nonce|iv|ct
 */
const hpnAuth = (() => {
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  // --- SHA-256 (FIPS 180-4) ------------------------------------------------
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

  function sha256(data) {
    const len = data.length;
    const blocks = Math.ceil((len + 9) / 64);
    const buf = new Uint8Array(blocks * 64);
    buf.set(data);
    buf[len] = 0x80;
    const view = new DataView(buf.buffer);
    view.setUint32(buf.length - 8, Math.floor(len / 0x20000000));
    view.setUint32(buf.length - 4, (len * 8) >>> 0);
    const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    const w = new Uint32Array(64);
    for (let off = 0; off < buf.length; off += 64) {
      for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
      for (let i = 16; i < 64; i++) {
        const a = w[i - 15], b = w[i - 2];
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let [A, B, C, D, E, F, G, H] = h;
      for (let i = 0; i < 64; i++) {
        const S1 = ((E >>> 6) | (E << 26)) ^ ((E >>> 11) | (E << 21)) ^ ((E >>> 25) | (E << 7));
        const ch = (E & F) ^ (~E & G);
        const t1 = (H + S1 + ch + K[i] + w[i]) >>> 0;
        const S0 = ((A >>> 2) | (A << 30)) ^ ((A >>> 13) | (A << 19)) ^ ((A >>> 22) | (A << 10));
        const maj = (A & B) ^ (A & C) ^ (B & C);
        const t2 = (S0 + maj) >>> 0;
        H = G; G = F; F = E; E = (D + t1) >>> 0; D = C; C = B; B = A; A = (t1 + t2) >>> 0;
      }
      h[0] += A; h[1] += B; h[2] += C; h[3] += D; h[4] += E; h[5] += F; h[6] += G; h[7] += H;
    }
    const out = new Uint8Array(32);
    const ov = new DataView(out.buffer);
    for (let i = 0; i < 8; i++) ov.setUint32(i * 4, h[i]);
    return out;
  }

  function concat(...parts) {
    const n = parts.reduce((s, p) => s + p.length, 0);
    const out = new Uint8Array(n);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  const bytes = (x) => (typeof x === 'string' ? enc.encode(x) : x);

  function hmac(key, msg) {
    key = bytes(key);
    if (key.length > 64) key = sha256(key);
    const k = new Uint8Array(64);
    k.set(key);
    const ipad = k.map((b) => b ^ 0x36);
    const opad = k.map((b) => b ^ 0x5c);
    return sha256(concat(opad, sha256(concat(ipad, bytes(msg)))));
  }

  // PBKDF2-HMAC-SHA256, single 32-byte block. The HMAC key schedule is hoisted
  // out of the loop, which makes 10 000 iterations take well under a second.
  function pbkdf2(password, salt, iter) {
    let key = bytes(password);
    if (key.length > 64) key = sha256(key);
    const k = new Uint8Array(64);
    k.set(key);
    const ipad = k.map((b) => b ^ 0x36);
    const opad = k.map((b) => b ^ 0x5c);
    const prf = (m) => sha256(concat(opad, sha256(concat(ipad, m))));
    let u = prf(concat(bytes(salt), new Uint8Array([0, 0, 0, 1])));
    const t = u.slice();
    for (let i = 1; i < iter; i++) {
      u = prf(u);
      for (let j = 0; j < 32; j++) t[j] ^= u[j];
    }
    return t;
  }

  const toHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  const fromHex = (h) => new Uint8Array((h.match(/../g) || []).map((x) => parseInt(x, 16)));
  const toB64 = (b) => btoa(String.fromCharCode(...b));
  const fromB64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const random = (n) => crypto.getRandomValues(new Uint8Array(n));   // available without HTTPS

  function deriveKeys(password, saltHex, iter) {
    const master = pbkdf2(password, fromHex(saltHex), iter);
    return { kAuth: hmac(master, 'hpn-auth'), kEnc: hmac(master, 'hpn-enc') };
  }

  // --- sealing (encrypt-then-MAC from HMAC-SHA256) ---------------------------
  function keystream(kEnc, iv, n) {
    const ks = hmac(kEnc, 'seal-stream');
    const out = new Uint8Array(n);
    for (let ctr = 0, o = 0; o < n; ctr++, o += 32) {
      const block = hmac(ks, concat(iv, new Uint8Array([ctr >>> 24, ctr >>> 16, ctr >>> 8, ctr])));
      out.set(block.subarray(0, Math.min(32, n - o)), o);
    }
    return out;
  }

  function seal(kEnc, nonce, plaintext) {
    const iv = random(16);
    const pt = bytes(plaintext);
    const ks = keystream(kEnc, iv, pt.length);
    const ct = pt.map((b, i) => b ^ ks[i]);
    const tag = hmac(hmac(kEnc, 'seal-mac'), concat(enc.encode(`HPN1-seal\n${nonce}\n`), iv, ct));
    return toB64(concat(iv, ct, tag));
  }

  function unseal(kEnc, nonce, b64) {
    const raw = fromB64(b64);
    if (raw.length < 48) throw new Error('sealed payload too short');
    const iv = raw.subarray(0, 16);
    const ct = raw.subarray(16, raw.length - 32);
    const tag = raw.subarray(raw.length - 32);
    const want = hmac(hmac(kEnc, 'seal-mac'), concat(enc.encode(`HPN1-seal\n${nonce}\n`), iv, ct));
    let diff = 0;
    for (let i = 0; i < 32; i++) diff |= want[i] ^ tag[i];
    if (diff) throw new Error('sealed payload failed its integrity check');
    const ks = keystream(kEnc, iv, ct.length);
    return dec.decode(ct.map((b, i) => b ^ ks[i]));
  }

  // --- request signing -------------------------------------------------------
  function canonicalQuery(search) {
    const pairs = [];
    for (const [k, v] of new URLSearchParams(search)) if (!k.startsWith('hpn_')) pairs.push([k, v]);
    pairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
    return pairs.map(([k, v]) => `${k}=${v}`).join('&');
  }

  function signature(kAuth, method, url, nonce, bodyHash) {
    const u = new URL(url, location.origin);
    const text = `HPN1\n${method.toUpperCase()}\n${u.pathname}\n${canonicalQuery(u.search)}\n${nonce}\n${bodyHash}`;
    return toHex(hmac(kAuth, text));
  }

  // --- session ------------------------------------------------------------------
  const STORE = 'hpn-session';
  let session = null;
  try { session = JSON.parse(sessionStorage.getItem(STORE) || 'null'); } catch (err) { session = null; }
  const nonces = [];

  async function challenge(user) {
    const r = await fetch(`/api/auth/challenge?user=${encodeURIComponent(user)}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`challenge failed (HTTP ${r.status})`);
    return r.json();
  }

  async function nextNonce() {
    if (nonces.length) return nonces.shift();
    const c = await challenge(session.user);
    return c.nonce;
  }

  function takeNonce(response) {
    const n = response.headers.get('X-HPN-Nonce');
    if (n) nonces.push(n);
  }

  async function login(user, password) {
    const c = await challenge(user);
    const k = deriveKeys(password, c.salt, c.iter);
    session = { user, kAuth: toHex(k.kAuth), kEnc: toHex(k.kEnc) };
    nonces.length = 0;
    nonces.push(c.nonce);
    const r = await authFetch('/api/session');
    if (!r.ok) {
      session = null;
      throw new Error(r.status === 423 ? 'too many failed logins, wait 30 s' : 'wrong user or password');
    }
    try { sessionStorage.setItem(STORE, JSON.stringify(session)); } catch (err) { /* private mode */ }
    return r.json();
  }

  function logout() {
    session = null;
    nonces.length = 0;
    try { sessionStorage.removeItem(STORE); } catch (err) { /* ignore */ }
  }

  // fetch() with the X-HPN-Auth header. `options.seal` encrypts a JSON body;
  // `options.unseal` decrypts a sealed JSON response (both bound to the nonce).
  async function authFetch(url, options = {}) {
    if (!session) throw Object.assign(new Error('not logged in'), { auth: true });
    for (let attempt = 0; attempt < 2; attempt++) {
      const nonce = await nextNonce();
      let body = options.body;
      if (options.seal) body = JSON.stringify({ sealed: seal(fromHex(session.kEnc), nonce, options.seal) });
      const method = (options.method || 'GET').toUpperCase();
      const bodyHash = toHex(sha256(body ? enc.encode(body) : new Uint8Array(0)));
      const sig = signature(fromHex(session.kAuth), method, url, nonce, bodyHash);
      const headers = { ...(options.headers || {}), 'X-HPN-Auth': `user=${session.user},nonce=${nonce},sig=${sig}` };
      if (options.seal) headers['Content-Type'] = 'application/json';
      const r = await fetch(url, { cache: 'no-store', ...options, method, body, headers });
      takeNonce(r);
      // A stale nonce (rebooted controller, evicted entry) is retried once.
      if (r.status === 401 && attempt === 0 && r.headers.get('X-HPN-Reason') === 'nonce') {
        nonces.length = 0;
        continue;
      }
      if (options.unseal && r.ok) {
        const data = await r.json();
        return { ok: true, status: r.status, json: async () => JSON.parse(unseal(fromHex(session.kEnc), nonce, data.sealed)) };
      }
      return r;
    }
    throw new Error('authentication failed');
  }

  // Signed header for an XMLHttpRequest upload: the image's SHA-256 goes into
  // the query (signed), the multipart body itself is marked "multipart".
  async function uploadAuth(url) {
    const nonce = await nextNonce();
    return `user=${session.user},nonce=${nonce},sig=${signature(fromHex(session.kAuth), 'POST', url, nonce, 'multipart')}`;
  }

  async function websocketUrl(path) {
    const nonce = await nextNonce();
    const sig = signature(fromHex(session.kAuth), 'GET', path, nonce, toHex(sha256(new Uint8Array(0))));
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}${path}?hpn_user=${session.user}&hpn_nonce=${nonce}&hpn_sig=${sig}`;
  }

  return {
    sha256, hmac, pbkdf2, deriveKeys, seal, unseal, toHex, fromHex, canonicalQuery,
    login, logout, authFetch, uploadAuth, websocketUrl, takeNonce,
    get user() { return session ? session.user : null; },
  };
})();
