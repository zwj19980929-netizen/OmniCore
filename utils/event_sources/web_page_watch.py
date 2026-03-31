"""
WebPageWatchSource — 定期抓取网页并检测内容变化。

实现策略：
1. 用 requests 轻量抓取目标页面文本
2. 与上次快照做文本 diff（hash + 词级变化率比较）
3. 变化超过阈值时生成 WatchEvent
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from utils.event_sources.base import WatchEvent
from utils.logger import log_agent_action, log_warning


# 最小检查间隔（秒），防止频率过高消耗资源
_MIN_CHECK_INTERVAL = 300  # 5 分钟


class WebPageWatchSource:
    """网页内容变化监控。"""

    def __init__(self, data_dir: str = "data"):
        self._watch_file = os.path.join(data_dir, "web_page_watches.jsonl")
        self._snapshot_dir = os.path.join(data_dir, "web_snapshots")
        os.makedirs(self._snapshot_dir, exist_ok=True)

    def get_source_type(self) -> str:
        return "web_page"

    def create_watch(self, config: Dict[str, Any]) -> str:
        """
        创建网页监控。

        config:
            url: 要监控的 URL
            session_id: 会话 ID
            check_interval_seconds: 检查间隔（默认 3600 = 1小时，最小 300）
            change_threshold: 变化阈值（0~1，默认 0.1 = 10% 变化触发）
            user_input_template: 触发时的任务描述
            extract_selector: CSS 选择器，只监控页面特定区域（可选）
            goal_id / project_id / todo_id: 可选上下文
            note: 备注
        """
        watch_id = f"web_watch_{uuid.uuid4().hex[:12]}"
        interval = max(config.get("check_interval_seconds", 3600), _MIN_CHECK_INTERVAL)

        watch = {
            "watch_id": watch_id,
            "url": config["url"],
            "session_id": config.get("session_id", ""),
            "check_interval_seconds": interval,
            "change_threshold": config.get("change_threshold", 0.1),
            "user_input_template": config.get(
                "user_input_template",
                "网页 {url} 内容发生了变化，请分析变化内容并汇报。",
            ),
            "extract_selector": config.get("extract_selector", ""),
            "goal_id": config.get("goal_id", ""),
            "project_id": config.get("project_id", ""),
            "todo_id": config.get("todo_id", ""),
            "note": config.get("note", ""),
            "status": "active",
            "last_snapshot_hash": "",
            "last_checked_at": "",
            "next_check_at": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
        }

        self._append_watch(watch)
        log_agent_action("WebPageWatch", f"Created watch: {watch_id} -> {config['url']}")
        return watch_id

    def poll_events(self, limit: int = 5) -> List[WatchEvent]:
        """检查所有到期的网页监控，返回变化事件。"""
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

            # 执行检查
            changed, new_hash, new_content = self._check_page(watch)

            if changed:
                url = watch["url"]
                template = watch.get(
                    "user_input_template",
                    f"网页 {url} 内容发生了变化，请分析。",
                )
                events.append(WatchEvent(
                    event_id=f"event_{uuid.uuid4().hex[:12]}",
                    source_type="web_page",
                    source_id=watch["watch_id"],
                    session_id=watch.get("session_id", ""),
                    trigger_data={
                        "url": url,
                        "new_hash": new_hash,
                        "old_hash": watch.get("last_snapshot_hash", ""),
                    },
                    user_input_template=template.format(url=url),
                    goal_id=watch.get("goal_id", ""),
                    project_id=watch.get("project_id", ""),
                    todo_id=watch.get("todo_id", ""),
                    created_at=now.isoformat(),
                ))

            # 保存新快照
            if new_content:
                self._save_snapshot(watch["watch_id"], new_content)

            # 更新检查时间
            interval = watch.get("check_interval_seconds", 3600)
            watch["last_checked_at"] = now.isoformat()
            watch["next_check_at"] = (now + timedelta(seconds=interval)).isoformat()
            watch["last_snapshot_hash"] = new_hash
            updated_watches.append(watch)

        self._save_watches(updated_watches)
        return events

    def _check_page(self, watch: Dict) -> Tuple[bool, str, str]:
        """抓取页面并与上次快照比较。返回 (changed, new_hash, new_content)。"""
        url = watch["url"]
        try:
            import requests
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; OmniCore/1.0)"
            })
            resp.raise_for_status()

            text = self._extract_text(resp.text, watch.get("extract_selector", ""))
            new_hash = hashlib.md5(text.encode()).hexdigest()
            old_hash = watch.get("last_snapshot_hash", "")

            if not old_hash:
                # 第一次检查，只记录快照不触发
                return False, new_hash, text

            if new_hash == old_hash:
                return False, new_hash, text

            # hash 不同，做文本相似度判断
            threshold = watch.get("change_threshold", 0.1)
            change_ratio = self._compute_change_ratio(watch["watch_id"], text)
            changed = change_ratio >= threshold

            return changed, new_hash, text

        except Exception as e:
            log_warning(f"WebPageWatch check failed for {url}: {e}")
            return False, watch.get("last_snapshot_hash", ""), ""

    def _extract_text(self, html: str, selector: str = "") -> str:
        """从 HTML 提取纯文本。"""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # 没有 beautifulsoup4，用简单正则去标签
            import re
            return re.sub(r"<[^>]+>", " ", html).strip()

        soup = BeautifulSoup(html, "html.parser")

        if selector:
            el = soup.select_one(selector)
            if el:
                return el.get_text(strip=True)

        # 去掉 script/style/nav/footer，取正文
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)

    def _compute_change_ratio(self, watch_id: str, new_content: str) -> float:
        """计算与上次快照的变化比例。"""
        snapshot_path = os.path.join(self._snapshot_dir, f"{watch_id}.txt")
        if not os.path.exists(snapshot_path):
            return 1.0

        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                old_content = f.read()
        except OSError:
            return 1.0

        old_words = set(old_content.split())
        new_words = set(new_content.split())
        if not old_words:
            return 1.0
        changed = len(old_words.symmetric_difference(new_words))
        total = max(len(old_words), len(new_words))
        return changed / total if total > 0 else 0.0

    def _save_snapshot(self, watch_id: str, content: str) -> None:
        """保存页面快照。"""
        snapshot_path = os.path.join(self._snapshot_dir, f"{watch_id}.txt")
        try:
            with open(snapshot_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            log_warning(f"Failed to save snapshot for {watch_id}: {e}")

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
        watches = self._load_watches()
        updated = []
        for w in watches:
            if w["watch_id"] == watch_id:
                # 清理快照文件
                snapshot_path = os.path.join(self._snapshot_dir, f"{watch_id}.txt")
                if os.path.exists(snapshot_path):
                    try:
                        os.remove(snapshot_path)
                    except OSError:
                        pass
                continue
            updated.append(w)
        self._save_watches(updated)

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
