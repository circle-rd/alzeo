#!/usr/bin/env python3
"""The embedding lane's installer and the proxy it depends on.

The first run on the reference box installed the unit and the drop-in, restarted the
proxy, and the route still reached the text engine: the box runs the copy install.sh put
in ~/.config/qwen38/keepalive-proxy.py, not the clone's, and a git update alone left the
old copy in memory. install-embed.sh now installs this branch's proxy when the deployed
one has no route at all. These are the static facts that keep that from regressing.
"""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (REPO / "install-embed.sh").read_text()
PROXY = (REPO / "keepalive-proxy.py").read_text()
UNIT = (REPO / "qwen38-embed.service.template").read_text()
LAUNCH = (REPO / "qwen38-embed-launch.sh.template").read_text()
LLAMA = (REPO / "qwen38-embed-llamacpp-launch.sh.template").read_text()


class TheRouteAndTheProxy(unittest.TestCase):
    def test_the_proxy_carries_both_spellings_and_the_upstream(self):
        for needle in ("EMBED_UPSTREAM", '"/v1/embeddings"', '"/v1/embedding"'):
            self.assertIn(needle, PROXY)
        # the branch is before the engine's own request handling, not after it
        self.assertIn('if self.command == "POST" and route in EMBED_ROUTES and EMBED_UPSTREAM:', PROXY)
        self.assertLess(PROXY.index('if self.command == "POST" and route in EMBED_ROUTES and EMBED_UPSTREAM:'),
                        PROXY.index("if body and self._rid_not_its_own():"))

    def test_the_installer_installs_the_proxys_route_when_the_deployed_copy_has_none(self):
        self.assertIn('$CONFIG_DIR/keepalive-proxy.py', INSTALLER)
        self.assertIn('install -m 755 "$HERE/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"', INSTALLER)
        self.assertLess(INSTALLER.index("install -m 755 \"$HERE/keepalive-proxy.py\""),
                        INSTALLER.index("Environment=EMBED_UPSTREAM"))
        self.assertIn('grep -q \'^EMBED_UPSTREAM\' "$HERE/keepalive-proxy.py"', INSTALLER)

    def test_the_proxy_timeout_outlives_a_full_batch(self):
        # 32,000 tokens took 86 s on the small cores; the default 60 s cut the connection
        self.assertIn('EMBED_TIMEOUT_S = float(os.environ.get("EMBED_TIMEOUT_S", "900"))', PROXY)
        self.assertIn("Environment=EMBED_TIMEOUT_S=900", INSTALLER)

    def test_the_default_batch_is_the_default_concurrency(self):
        # TEI takes one permit per input, not per request: 8 permits answered 429 on a
        # 64-input index batch (reference box, 2026-11-17)
        self.assertIn('EMBED_CONCURRENCY="${EMBED_CONCURRENCY:-64}"', INSTALLER)
        self.assertIn("per INPUT", INSTALLER)
        self.assertIn("--max-client-batch-size __EMBED_CLIENT_BATCH__", LAUNCH)
        self.assertIn("--max-concurrent-requests __EMBED_CONCURRENCY__", LAUNCH)

    def test_the_llamacpp_lane_is_the_default_and_tei_the_fallback(self):
        self.assertIn('EMBED_ENGINE="${ENGINE_ARG:-${EMBED_ENGINE:-llamacpp}}"', INSTALLER)
        self.assertIn("--engine tei", INSTALLER)
        self.assertIn("llamacpp|tei", INSTALLER)

    def test_the_llamacpp_launcher_has_the_two_mandatory_settings(self):
        # the GGUF records causal attention and pooling -1; both overrides are verified
        self.assertIn("--embedding", LLAMA)
        self.assertIn("--pooling mean", LLAMA)
        self.assertIn("--override-kv qwen3.attention.causal=bool:false", LLAMA)
        self.assertIn("-c __EMBED_CTX__ -b __EMBED_CTX__ -ub __EMBED_CTX__", LLAMA)
        self.assertIn("-ngl 999 -fa on", LLAMA)
        self.assertIn("--api-key-file /out/api-key", LLAMA)
        self.assertIn("--host 127.0.0.1 --port __EMBED_PORT__", LLAMA)
        self.assertIn("--embd-normalize 2", LLAMA)
        # the image checks localhost:8080; this lane listens on 30027
        self.assertIn("--no-healthcheck", LLAMA)

    def test_the_gguf_is_pinned_by_repository_revision_and_hash(self):
        self.assertIn('EMBED_GGUF_REPO="${EMBED_GGUF_REPO:-bowang0911/pplx-embed-v1-0.6b-gguf}"', INSTALLER)
        self.assertIn('EMBED_GGUF_REV="${EMBED_GGUF_REV:-ab95a3d1159624bed3ff10ae4821ff2ae2ada2ce}"', INSTALLER)
        self.assertIn('EMBED_GGUF_SHA="${EMBED_GGUF_SHA:-08ba3d20e89fe98cd8d129381424dfba1951d35de32a7ec7869fd77733c300eb}"', INSTALLER)
        self.assertIn('sha256sum -c -', INSTALLER)
        self.assertIn('resolve/$EMBED_GGUF_REV/$EMBED_GGUF_FILE', INSTALLER)

    def test_the_proxy_is_not_in_conflict_with_a_text_lane(self):
        self.assertFalse([ln for ln in UNIT.splitlines() if ln.startswith("Conflicts=")])
        for launch in (LAUNCH, LLAMA):
            self.assertIn("--memory __EMBED_MEM_MAX__", launch)
            self.assertIn("--cpuset-cpus __EMBED_CPUS__", launch)
        self.assertIn("--max-batch-tokens __EMBED_BATCH_TOKENS__", LAUNCH)
        self.assertIn("--served-model-name __EMBED_NAME__", LAUNCH)


if __name__ == "__main__":
    unittest.main()
