#!/usr/bin/env python3
import base64
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GerritError(RuntimeError):
    pass


class GerritClient:
    def __init__(self, base_url, username=None, password=None, verify_ssl=True, logger=None):
        self.base_url = base_url.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.logger = logger
        self.context = None if verify_ssl else ssl._create_unverified_context()

    @classmethod
    def from_config(cls, config, logger=None):
        g = config.get("gerrit", {})
        env_name = g.get("http_password_env", "GERRIT_HTTP_PASSWORD")

        # POC 优先读取 config.json 中直接配置的 Gerrit HTTP Password。
        # 若未配置，再回退到环境变量，方便后续生产化时避免明文凭据。
        password = g.get("http_password") or os.environ.get(env_name, "")

        return cls(
            g.get("base_url", ""),
            g.get("username"),
            password,
            bool(g.get("verify_ssl", True)),
            logger,
        )

    def _url(self, path):
        prefix = "/a" if self.username and self.password else ""
        return self.base_url + prefix + path

    def _request(self, path, accept="application/json"):
        url = self._url(path)
        headers = {"Accept": accept, "User-Agent": "SkillHub-Gerrit-Change-POC/0.1"}
        if self.username and self.password:
            token = base64.b64encode((self.username + ":" + self.password).encode("utf-8")).decode("ascii")
            headers["Authorization"] = "Basic " + token
        if self.logger:
            self.logger.debug("Gerrit GET %s", url)
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, context=self.context, timeout=30) as resp:
                return resp.read(), dict(resp.headers)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise GerritError("Gerrit HTTP {}: {}\n{}".format(exc.code, url, body))
        except URLError as exc:
            raise GerritError("Gerrit request failed: {}: {}".format(url, exc))

    @staticmethod
    def _parse_json(raw):
        text = raw.decode("utf-8", "replace")
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return json.loads(text)

    def get_change_detail(self, change_id):
        cid = quote(str(change_id), safe="")
        raw, _ = self._request("/changes/{}/detail?o=CURRENT_REVISION".format(cid))
        return self._parse_json(raw)

    def get_revision_files(self, change_id, revision="current"):
        cid = quote(str(change_id), safe="")
        rid = quote(str(revision), safe="")
        raw, _ = self._request("/changes/{}/revisions/{}/files/".format(cid, rid))
        files = self._parse_json(raw)
        return {path: info for path, info in files.items() if not path.startswith("/")}

    def get_file_content(self, change_id, revision, file_path, parent=None):
        cid = quote(str(change_id), safe="")
        rid = quote(str(revision), safe="")
        fid = quote(file_path, safe="")
        suffix = "?parent={}".format(int(parent)) if parent else ""
        raw, _ = self._request(
            "/changes/{}/revisions/{}/files/{}/content{}".format(cid, rid, fid, suffix),
            accept="text/plain",
        )
        try:
            return base64.b64decode(raw.strip())
        except Exception as exc:
            raise GerritError("failed to decode Gerrit file content {}: {}".format(file_path, exc))

    @staticmethod
    def current_revision_info(change_detail):
        revision = change_detail.get("current_revision")
        revisions = change_detail.get("revisions", {})
        if not revision or revision not in revisions:
            raise GerritError("change response has no current revision")
        info = revisions[revision]
        return revision, info
