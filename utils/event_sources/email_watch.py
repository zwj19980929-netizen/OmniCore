"""
EmailWatchSource — IMAP 邮件监控。

依赖：imaplib（标准库）

注意：邮件密码应通过环境变量引用，不存明文。
配置中 password 字段支持 ${ENV_VAR} 语法，运行时自动解析。
"""

import email
import email.header
import imaplib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from utils.event_sources.base import WatchEvent
from utils.logger import log_agent_action, log_warning


_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")


def _resolve_env(value: str) -> str:
    """解析 ${ENV_VAR} 引用。"""
    m = _ENV_REF_RE.match(value.strip())
    if m:
        return os.getenv(m.group(1), "")
    return value


class EmailWatchSource:
    """IMAP 邮件监控事件源。"""

    def __init__(self, data_dir: str = "data"):
        self._watch_file = os.path.join(data_dir, "email_watches.jsonl")

    def get_source_type(self) -> str:
        return "email"

    def create_watch(self, config: Dict[str, Any]) -> str:
        """
        创建邮件监控。

        config:
            imap_host: IMAP 服务器地址
            imap_port: IMAP 端口（默认 993）
            username: 用户名
            password: 密码（建议使用 ${ENV_VAR} 引用）
            use_ssl: 是否使用 SSL（默认 true）
            mailbox: 监控的邮箱文件夹（默认 INBOX）
            subject_filter: 主题关键词过滤（可选）
            sender_filter: 发件人过滤（可选）
            check_interval_seconds: 检查间隔（默认 600 = 10 分钟）
            user_input_template: 触发任务描述
            session_id: 会话 ID
        """
        watch_id = f"email_watch_{uuid.uuid4().hex[:12]}"
        watch = {
            "watch_id": watch_id,
            "imap_host": config["imap_host"],
            "imap_port": config.get("imap_port", 993),
            "username": config["username"],
            "password": config["password"],  # 应为 ${ENV_VAR} 格式
            "use_ssl": config.get("use_ssl", True),
            "mailbox": config.get("mailbox", "INBOX"),
            "subject_filter": config.get("subject_filter", ""),
            "sender_filter": config.get("sender_filter", ""),
            "check_interval_seconds": max(config.get("check_interval_seconds", 600), 60),
            "user_input_template": config.get(
                "user_input_template",
                "收到新邮件：主题「{subject}」，发件人：{sender}。请处理。",
            ),
            "session_id": config.get("session_id", ""),
            "goal_id": config.get("goal_id", ""),
            "project_id": config.get("project_id", ""),
            "todo_id": config.get("todo_id", ""),
            "status": "active",
            "last_uid": "",
            "last_checked_at": "",
            "next_check_at": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
        }
        self._append_watch(watch)
        log_agent_action("EmailWatch", f"Created watch: {watch_id} -> {config['imap_host']}")
        return watch_id

    def poll_events(self, limit: int = 5) -> List[WatchEvent]:
        """检查新邮件，返回匹配的事件。"""
        watches = self._load_watches()
        now = datetime.now()
        events: List[WatchEvent] = []
        updated_watches: List[Dict] = []

        for watch in watches:
            if watch.get("status") != "active":
                updated_watches.append(watch)
                continue

            next_check = watch.get("next_check_at", "")
            if next_check:
                try:
                    if datetime.fromisoformat(next_check) > now:
                        updated_watches.append(watch)
                        continue
                except ValueError:
                    pass

            if len(events) >= limit:
                updated_watches.append(watch)
                continue

            try:
                new_mails, new_last_uid = self._fetch_new_mails(watch)
                for mail_info in new_mails[:limit - len(events)]:
                    template = watch.get(
                        "user_input_template",
                        "收到新邮件：主题「{subject}」，发件人：{sender}。请处理。",
                    )
                    events.append(WatchEvent(
                        event_id=f"event_{uuid.uuid4().hex[:12]}",
                        source_type="email",
                        source_id=watch["watch_id"],
                        session_id=watch.get("session_id", ""),
                        trigger_data=mail_info,
                        user_input_template=template.format(
                            subject=mail_info.get("subject", ""),
                            sender=mail_info.get("sender", ""),
                            snippet=mail_info.get("snippet", ""),
                        ),
                        goal_id=watch.get("goal_id", ""),
                        project_id=watch.get("project_id", ""),
                        todo_id=watch.get("todo_id", ""),
                        created_at=now.isoformat(),
                    ))
                if new_last_uid:
                    watch["last_uid"] = new_last_uid
            except Exception as e:
                log_warning(f"EmailWatch poll failed for {watch['watch_id']}: {e}")

            interval = watch.get("check_interval_seconds", 600)
            watch["last_checked_at"] = now.isoformat()
            watch["next_check_at"] = (now + timedelta(seconds=interval)).isoformat()
            updated_watches.append(watch)

        self._save_watches(updated_watches)
        return events

    def _fetch_new_mails(self, watch: Dict) -> tuple:
        """通过 IMAP 获取新邮件。返回 (mail_list, last_uid)。"""
        host = watch["imap_host"]
        port = watch.get("imap_port", 993)
        username = _resolve_env(watch["username"])
        password = _resolve_env(watch["password"])
        use_ssl = watch.get("use_ssl", True)
        mailbox = watch.get("mailbox", "INBOX")
        last_uid = watch.get("last_uid", "")

        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)

        try:
            conn.login(username, password)
            conn.select(mailbox, readonly=True)

            # 搜索新邮件
            if last_uid:
                status, data = conn.uid("search", None, f"UID {int(last_uid) + 1}:*")
            else:
                # 首次：只取最近 1 天的邮件
                since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
                status, data = conn.uid("search", None, f"SINCE {since_date}")

            if status != "OK" or not data[0]:
                return [], last_uid

            uid_list = data[0].split()
            # 过滤掉 last_uid 本身（IMAP UID 范围包含下界）
            if last_uid:
                uid_list = [u for u in uid_list if int(u) > int(last_uid)]

            mails = []
            new_last_uid = last_uid

            for uid_bytes in uid_list[-10:]:  # 最多处理 10 封
                uid_str = uid_bytes.decode() if isinstance(uid_bytes, bytes) else str(uid_bytes)
                status, msg_data = conn.uid("fetch", uid_str, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = self._decode_header(msg.get("Subject", ""))
                sender = self._decode_header(msg.get("From", ""))

                # 应用过滤条件
                subject_filter = watch.get("subject_filter", "")
                if subject_filter and subject_filter.lower() not in subject.lower():
                    new_last_uid = uid_str
                    continue

                sender_filter = watch.get("sender_filter", "")
                if sender_filter and sender_filter.lower() not in sender.lower():
                    new_last_uid = uid_str
                    continue

                # 提取正文摘要
                snippet = self._extract_snippet(msg)

                mails.append({
                    "uid": uid_str,
                    "subject": subject,
                    "sender": sender,
                    "snippet": snippet,
                    "date": msg.get("Date", ""),
                })
                new_last_uid = uid_str

            return mails, new_last_uid
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _decode_header(self, raw: str) -> str:
        """解码邮件头。"""
        if not raw:
            return ""
        decoded_parts = email.header.decode_header(raw)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result)

    def _extract_snippet(self, msg: email.message.Message, max_len: int = 200) -> str:
        """提取邮件正文摘要。"""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        return text[:max_len].strip()
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                return text[:max_len].strip()
        return ""

    def list_watches(self) -> List[Dict[str, Any]]:
        return [w for w in self._load_watches() if w.get("status") != "deleted"]

    def pause_watch(self, watch_id: str) -> None:
        watches = self._load_watches()
        for w in watches:
            if w["watch_id"] == watch_id:
                w["status"] = "paused"
        self._save_watches(watches)

    def resume_watch(self, watch_id: str) -> None:
        watches = self._load_watches()
        for w in watches:
            if w["watch_id"] == watch_id:
                w["status"] = "active"
                w["next_check_at"] = datetime.now().isoformat()
        self._save_watches(watches)

    def delete_watch(self, watch_id: str) -> None:
        watches = [w for w in self._load_watches() if w["watch_id"] != watch_id]
        self._save_watches(watches)

    def _load_watches(self) -> List[Dict]:
        if not os.path.exists(self._watch_file):
            return []
        watches = []
        with open(self._watch_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        watches.append(json.loads(line))
                    except (json.JSONDecodeError, ValueError):
                        pass
        return watches

    def _append_watch(self, watch: Dict) -> None:
        os.makedirs(os.path.dirname(self._watch_file) or ".", exist_ok=True)
        with open(self._watch_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(watch, ensure_ascii=False) + "\n")

    def _save_watches(self, watches: List[Dict]) -> None:
        os.makedirs(os.path.dirname(self._watch_file) or ".", exist_ok=True)
        with open(self._watch_file, "w", encoding="utf-8") as f:
            for w in watches:
                f.write(json.dumps(w, ensure_ascii=False) + "\n")
