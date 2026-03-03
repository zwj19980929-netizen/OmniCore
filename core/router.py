"""
OmniCore 涓昏剳璺敱鍣?Agent
璐熻矗鎺ユ敹鐢ㄦ埛鎸囦护锛岃瘑鍒剰鍥撅紝鎷嗚В涓哄瓙浠诲姟 DAG
"""
import json
from typing import List, Dict, Any

from core.constants import TaskType
from core.state import OmniCoreState, TaskItem
from core.task_planner import build_task_item_from_plan
from core.llm import LLMClient
from utils.logger import log_agent_action, logger


# Router Agent 鐨勭郴缁熸彁绀鸿瘝
ROUTER_SYSTEM_PROMPT = """浣犳槸 OmniCore 鐨勪富鑴戣矾鐢卞櫒銆備綘鏄竴涓仾鏄庣殑銆佹湁鐙珛鎬濊€冭兘鍔涚殑 AI 璋冨害涓績銆?

## 浣犵殑鏍稿績鑳藉姏
浣犺兘鐞嗚В鐢ㄦ埛鐨勮嚜鐒惰瑷€鎸囦护锛岃嚜涓诲垎鏋愭剰鍥撅紝骞跺皢浠诲姟鏅鸿兘鎷嗚В涓哄彲鎵ц鐨勫瓙浠诲姟銆備綘涓嶆槸涓€涓鏉跨殑瑙勫垯寮曟搸锛岃€屾槸涓€涓兘鎺ㄧ悊銆佽兘鍒ゆ柇銆佽兘鐏垫椿搴斿彉鐨勬櫤鑳藉ぇ鑴戙€?

## 鎬濊€冩柟寮?
鏀跺埌鐢ㄦ埛鎸囦护鍚庯紝璇锋寜浠ヤ笅鏂瑰紡鎬濊€冿細
1. 鐢ㄦ埛鍒板簳鎯宠浠€涔堬紵锛堜笉瑕佸彧鐪嬪瓧闈㈡剰鎬濓紝鐞嗚В娣卞眰鎰忓浘锛?
2. 瀹屾垚杩欎欢浜嬮渶瑕佸摢浜涙楠わ紵锛堣嚜涓昏鍒掞紝涓嶈濂楁ā鏉匡級
3. 姣忎釜姝ラ搴旇浜ょ粰璋佹潵鍋氾紵锛堥€夋嫨鏈€鍚堥€傜殑 Worker锛?
4. 姝ラ涔嬮棿鏈変粈涔堜緷璧栧叧绯伙紵锛堝厛鍋氫粈涔堝悗鍋氫粈涔堬級
5. 姣忔潯璺緞璧板緱閫氬悧锛熸湁娌℃湁鏇寸渷鍔涚殑鏇夸唬璺緞锛燂紙棰勫垽闅滅锛岄€夋嫨闃诲姏鏈€灏忕殑璺級

## 璺緞閫夋嫨鎬濈淮锛堥噸瑕侊級
鍦ㄨ鍒掑叿浣撴楠や箣鍓嶏紝鍏堟兂涓€鎯?淇℃伅浠庡摢鏉ユ渶瀹规槗鎷垮埌"锛?
- 鍚屼竴涓洰鏍囷紝寰€寰€鏈夊鏉¤矾寰勫彲浠ヨ揪鎴愩€備綘搴旇浼樺厛閫夋嫨闃诲姏鏈€灏忋€佹垚鍔熺巼鏈€楂樼殑閭ｆ潯銆?
- 濡傛灉鏌愭潯璺緞澶ф鐜囧瓨鍦ㄩ殰纰嶏紙闇€瑕佺櫥褰曘€侀渶瑕佷粯璐广€侀渶瑕佸鏉備氦浜掞級锛屽厛鎯虫兂鏈夋病鏈夋洿寮€鏀剧殑鏇夸唬鏉ユ簮鑳借揪鍒板悓鏍风洰鐨勩€?
- 褰撲綘涓嶇‘瀹氭渶浣宠矾寰勬椂锛屽彲浠ュ厛瀹夋帓涓€涓悳绱㈡楠わ紝璁?Worker 閫氳繃鎼滅储寮曟搸鎵惧埌鏈€鍚堥€傜殑淇℃伅鏉ユ簮锛屽啀鍩轰簬鎼滅储缁撴灉鎵ц鍚庣画浠诲姟銆?
- 涓嶈鎵х潃浜?鏈€瀹樻柟"鎴?鏈€鐩存帴"鐨勬潵婧愶紝鐢ㄦ埛瑕佺殑鏄粨鏋滐紝涓嶆槸杩囩▼銆傝兘鎷垮埌鍑嗙‘淇℃伅鐨勮矾寰勫氨鏄ソ璺緞銆?

鍦?fallbacks 涓篃瑕佷綋鐜拌繖绉嶆€濈淮锛氱涓€灞?fallback 鍙互鏄崲鍙傛暟閲嶈瘯锛屼絾鑷冲皯鏈変竴灞?fallback 搴旇鏄?鎹竴鏉″畬鍏ㄤ笉鍚岀殑璺緞"鈥斺€旀瘮濡傛崲淇℃伅鏉ユ簮銆佹崲鎼滅储鏂瑰紡銆佹垨鑰呰 Worker 鑷繁鍘绘悳绱€?

## 鍙敤鐨?Worker
- web_worker: 缃戦〉鏁版嵁鎶撳彇锛堟墦寮€缃戦〉銆佹彁鍙栧唴瀹癸紝鍙鎿嶄綔锛?
- browser_agent: 鏅鸿兘娴忚鍣ㄤ唬鐞嗭紙闇€瑕佷氦浜掔殑浠诲姟锛氳喘鐗┿€佺櫥褰曘€佸～琛ㄣ€佹悳绱㈢瓑锛?
- file_worker: 鏈湴鏂囦欢璇诲啓锛堜繚瀛樻暟鎹€佽鍙栨枃浠躲€佺敓鎴愭姤鍛婏級
- system_worker: 绯荤粺绾ф搷浣滐紙鎵ц鍛戒护銆佹搷浣滃簲鐢ㄧ▼搴忥級

## Worker 閫夋嫨鍘熷垯
- 鍙渶瑕佺湅缃戦〉銆佹姄鏁版嵁 鈫?web_worker
- 闇€瑕佺偣鍑汇€佽緭鍏ャ€佸姝ヤ氦浜?鈫?browser_agent
- 闇€瑕佽鍐欐湰鍦版枃浠?鈫?file_worker
- 闇€瑕佹墽琛岀郴缁熷懡浠?鈫?system_worker
- 鍙槸闂棶棰樸€佽亰澶┿€佹煡鍘嗗彶 鈫?涓嶉渶瑕?Worker锛岀洿鎺ュ湪 reasoning 涓洖绛?

## 杈撳嚭鏍煎紡锛堝繀椤绘槸鏈夋晥鐨?JSON锛?
{
    "intent": "浣犲垽鏂殑鎰忓浘绫诲瀷锛堣嚜鐢辨弿杩帮紝濡?web_scraping / file_operation / information_query 绛夛級",
    "confidence": 0.95,
    "reasoning": "浣犵殑瀹屾暣鎬濊€冭繃绋?,
    "tasks": [
        {
            "task_id": "task_1",
            "tool_name": "web.fetch_and_extract",
            "task_type": "web_worker",
            "description": "娓呮櫚瀹屾暣鐨勪换鍔℃弿杩帮紝Worker 鎷垮埌灏辫兘鎵ц",
            "tool_args": {"url": "", "limit": 5},
            "params": {"url": "", "limit": 5},
            "priority": 10,
            "depends_on": [],
            "required_capabilities": ["text_chat"],
            "success_criteria": ["鎻忚堪鎴愬姛鐨勫彲楠岃瘉鏉′欢"],
            "fallbacks": [{"type": "retry", "param_patch": {}}],
            "abort_conditions": ["浠€涔堟儏鍐典笅搴旇鏀惧純"]
        }
    ],
    "is_high_risk": false,
    "high_risk_reason": ""
}

## success_criteria 缂栧啓鎸囧崡
success_criteria 鏄?Worker 鎵ц鍚庣敤浜庤嚜鍔ㄩ獙璇佺粨鏋滅殑鏉′欢鍒楄〃锛屾瘡鏉℃槸涓€涓彲姹傚€肩殑 Python 琛ㄨ揪寮忋€?
- 濂界殑渚嬪瓙锛歚len(result.data) >= 5`銆乣result.success == True`銆乣'file_path' in result`
- 鍧忕殑渚嬪瓙锛歚result.success == true`锛堣繖澶硾浜嗭紝鍑犱箮绛変簬娌″啓锛?
- 閽堝 web_worker锛氬啓鏄庢湡鏈涚殑鏁版嵁鏉℃暟锛屽 `len(result.data) >= 5`
- 閽堝 file_worker锛氬啓鏄庢枃浠跺繀椤诲瓨鍦紝濡?`result.file_path and result.success`
- 閽堝 system_worker锛氬啓鏄庤繑鍥炵爜锛屽 `result.return_code == 0`

## fallback 绛栫暐璇存槑
fallbacks 鏄竴涓湁搴忓垪琛紝Worker 澶辫触鏃舵寜椤哄簭灏濊瘯锛?
- `{"type": "retry", "param_patch": {"headless": false}}` 鈥?鐢ㄤ慨鏀瑰悗鐨勫弬鏁伴噸璇曞悓涓€涓?Worker
- `{"type": "switch_worker", "target": "browser_agent", "param_patch": {"task": "..."}}` 鈥?鍒囨崲鍒板彟涓€涓?Worker 绫诲瀷鎵ц
- 濡傛灉浣犱笉纭畾闇€瑕佷粈涔?fallback锛屽彲浠ョ暀绌哄垪琛?[]

## params 鍙傝€冿紙涓嶆槸姝昏鍒欙紝鏍规嵁瀹為檯闇€瑕佺伒娲诲～鍐欙級

web_worker params:
- url: 鐩爣 URL锛堝鏋滀綘鐭ラ亾鐨勮瘽锛涗笉纭畾灏辩暀绌猴紝Worker 浼氳嚜宸辨悳绱級
- limit: 鎶撳彇鏁伴噺闄愬埗

browser_agent params:
- task: 瀹屾暣鐨勪换鍔℃弿杩?
- start_url: 璧峰 URL锛堝彲閫夛級
- headless: 鏄惁鏃犲ご妯″紡

file_worker params:
- action: "write" 鎴?"read"
- file_path: 鏂囦欢璺緞
- data_source: 鏁版嵁鏉ユ簮鐨?task_id锛堢敤浜庡啓鍏ヤ粠鍏朵粬浠诲姟鑾峰彇鐨勬暟鎹級
- data_sources: 澶氫釜鏁版嵁鏉ユ簮鐨?task_id 鍒楄〃锛堝婧愬姣斿満鏅級
- format: 杈撳嚭鏍煎紡锛坱xt/xlsx/csv/markdown/html锛屾牴鎹満鏅櫤鑳介€夋嫨锛?

## 鍏抽敭鍘熷垯
1. 鐏垫椿鎬濊€冿紝涓嶈姝绘澘濂楃敤瑙勫垯
2. 浠诲姟鎻忚堪瑕佸啓娓呮锛岃 Worker 鎷垮埌灏辫兘骞叉椿
3. 涓嶇‘瀹?URL 鏃朵笉瑕佺瀻鐚滐紝璁?Worker 鑷繁鍘绘悳绱?
4. 娉ㄦ剰鍖哄垎鍚嶇О鐩镐技浣嗕笉鍚岀殑浜嬬墿锛堥潬浣犵殑鎺ㄧ悊鑳藉姏鍒ゆ柇锛?
5. 娑夊強浠樻銆佸垹闄ゃ€佸彂閫佺瓑涓嶅彲閫嗘搷浣滄椂锛屾爣璁?is_high_risk
6. 鏂囦欢鏍煎紡鏍规嵁鍦烘櫙鏅鸿兘閫夋嫨锛氭暟鎹姣旂敤 xlsx锛屾姤鍛婄敤 html锛岀畝鍗曟枃鏈敤 txt
7. 濡傛灉鐢ㄦ埛鍙槸鍦ㄩ棶闂鎴栬亰澶╋紝涓嶉渶瑕佸垱寤轰换鍔★紝鐩存帴鍦?reasoning 涓洖绛?

## 鑳藉姏鏍囨敞瑕佹眰
鍦ㄥ垎鏋愪换鍔℃椂锛屼綘闇€瑕佷负姣忎釜瀛愪换鍔℃爣娉ㄦ墍闇€鐨勬ā鍨嬭兘鍔涳紙required_capabilities锛夈€?
绯荤粺浼氭牴鎹繖浜涙爣绛捐嚜鍔ㄩ€夋嫨鏈€鍚堥€傜殑瀛愭ā鍨嬨€?

鍙敤鐨勮兘鍔涚被鍨嬶細
- text_chat: 鍩虹鏂囨湰瀵硅瘽
- text_long: 闀挎枃鏈鐞嗭紙瓒呰繃 32k tokens 鐨勫唴瀹癸級
- vision: 鍥剧墖鐞嗚В/OCR/鎴浘鍒嗘瀽
- image_gen: 鍥剧墖鐢熸垚/缁樼敾
- stt: 璇煶璇嗗埆锛堥煶棰戣浆鏂囧瓧锛?
- tts: 璇煶鍚堟垚锛堟枃瀛楄浆璇煶锛?
- code: 浠ｇ爜鐢熸垚/璋冭瘯
- reasoning: 澶嶆潅鎺ㄧ悊/鏁板/閫昏緫

澶у鏁颁换鍔″彧闇€瑕?text_chat锛屽彧鏈夋槑纭秹鍙婂浘鐗囥€佽闊炽€侀暱鏂囨。绛夊満鏅墠闇€瑕佹爣娉ㄥ叾浠栬兘鍔涖€?

## 瀵硅瘽涓婁笅鏂?
濡傛灉鎻愪緵浜嗗璇濆巻鍙诧紝缁撳悎鍘嗗彶鐞嗚В鐢ㄦ埛鎰忓浘銆傜敤鎴峰彲鑳藉湪杩介棶涔嬪墠鐨勬搷浣滅粨鏋滐紙濡?鏂囦欢鍦ㄥ摢"銆?鍒氭墠鐨勬暟鎹?锛夛紝杩欐椂鐩存帴浠庡巻鍙蹭腑鎵剧瓟妗堬紝鐢?information_query 鎰忓浘鍥炵瓟鍗冲彲銆?
"""

ROUTER_OUTPUT_APPENDIX = """
## Tool Planning Output Upgrade
- Prefer `tool_name` and `tool_args` for each task.
- Keep `task_type` only for backward compatibility.
- Valid tool_name values:
  - web.fetch_and_extract
  - browser.interact
  - file.read_write
  - system.control
"""


class RouterAgent:
    """
    涓昏剳璺敱鍣?Agent
    璐熻矗鎰忓浘璇嗗埆鍜屼换鍔℃媶瑙?
    """

    def __init__(self, llm_client: LLMClient = None):
        self.llm = llm_client or LLMClient()
        self.name = "Router"

    @staticmethod
    def _normalize_task_plan_shape(result: Dict[str, Any]) -> Dict[str, Any]:
        tool_to_task = {
            "web.fetch_and_extract": str(TaskType.WEB_WORKER),
            "browser.interact": str(TaskType.BROWSER_AGENT),
            "file.read_write": str(TaskType.FILE_WORKER),
            "system.control": str(TaskType.SYSTEM_WORKER),
        }
        task_to_tool = {value: key for key, value in tool_to_task.items()}

        normalized_tasks = []
        for raw_task in result.get("tasks", []) or []:
            task_data = dict(raw_task)
            tool_name = str(task_data.get("tool_name", "") or "").strip()
            task_type = str(task_data.get("task_type", "") or "").strip()

            if not tool_name and task_type:
                tool_name = task_to_tool.get(task_type, "")
            if not task_type and tool_name:
                task_type = tool_to_task.get(tool_name, "")

            tool_args = task_data.get("tool_args")
            params = task_data.get("params")
            if isinstance(tool_args, dict):
                task_data["tool_args"] = tool_args
                task_data["params"] = dict(tool_args)
            elif isinstance(params, dict):
                task_data["params"] = params
                task_data["tool_args"] = dict(params)
            else:
                task_data["params"] = {}
                task_data["tool_args"] = {}

            task_data["tool_name"] = tool_name
            task_data["task_type"] = task_type
            normalized_tasks.append(task_data)

        result["tasks"] = normalized_tasks
        return result

    def analyze_intent(
        self,
        user_input: str,
        conversation_history: list = None,
        related_history: list = None,
    ) -> Dict[str, Any]:
        """
        鍒嗘瀽鐢ㄦ埛鎰忓浘骞舵媶瑙ｄ换鍔?

        Args:
            user_input: 鐢ㄦ埛鍘熷杈撳叆
            conversation_history: 鏈€杩戠殑瀵硅瘽鍘嗗彶
            related_history: 鍚戦噺妫€绱㈠埌鐨勭浉鍏冲巻鍙茶蹇?

        Returns:
            鍖呭惈鎰忓浘鍜屼换鍔″垪琛ㄧ殑瀛楀吀
        """
        log_agent_action(self.name, "开始分析用户意图", user_input[:50] + "...")

        # 鏋勫缓鍖呭惈瀵硅瘽鍘嗗彶鐨勭敤鎴锋秷鎭?
        user_message = ""
        if conversation_history:
            history_lines = []
            for turn in conversation_history:
                history_lines.append(f"鐢ㄦ埛: {turn['user_input']}")
                history_lines.append(f"缁撴灉: {'鎴愬姛' if turn.get('success') else '澶辫触'} - {turn.get('output', '')[:150]}")
            user_message += "## 鏈€杩戠殑瀵硅瘽鍘嗗彶锛堢敤浜庣悊瑙ｄ笂涓嬫枃锛夛細\n"
            user_message += "\n".join(history_lines)
            user_message += "\n\n---\n"

        if related_history:
            memory_lines = []
            for memory in related_history[:3]:
                content = str(memory.get("content", "")).replace("\n", " ").strip()
                if content:
                    memory_lines.append(f"- {content[:220]}")
            if memory_lines:
                user_message += "## 鐩稿叧鍘嗗彶璁板繂锛堝彲鐢ㄤ簬澶嶇敤涓婁笅鏂囨垨鐩存帴鍥炵瓟杩介棶锛夛細\n"
                user_message += "\n".join(memory_lines)
                user_message += "\n\n---\n"

        user_message += f"璇峰垎鏋愪互涓嬬敤鎴锋寚浠ゅ苟鎷嗚В浠诲姟锛歕n\n{user_input}"

        response = self.llm.chat_with_system(
            system_prompt=f"{ROUTER_SYSTEM_PROMPT}\n\n{ROUTER_OUTPUT_APPENDIX}",
            user_message=user_message,
            temperature=0.3,
            max_tokens=16000,
            json_mode=True,
        )

        logger.debug(f"Router LLM 鍘熷鍝嶅簲: {response.content[:300] if response.content else '(绌?'}")

        try:
            result = self._normalize_task_plan_shape(
                self.llm.parse_json_response(response)
            )
            log_agent_action(
                self.name,
                f"鎰忓浘璇嗗埆瀹屾垚: {result.get('intent')}",
                f"缃俊搴? {result.get('confidence', 0):.2f}, 瀛愪换鍔℃暟: {len(result.get('tasks', []))}"
            )
            return result
        except Exception as e:
            logger.error(f"Router 瑙ｆ瀽澶辫触: {e}")
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "reasoning": f"瑙ｆ瀽澶辫触: {str(e)}",
                "tasks": [],
                "is_high_risk": False,
            }

    def route(self, state: OmniCoreState) -> OmniCoreState:
        """
        LangGraph 鑺傜偣鍑芥暟锛氭墽琛岃矾鐢遍€昏緫

        Args:
            state: 褰撳墠鍥剧姸鎬?

        Returns:
            鏇存柊鍚庣殑鐘舵€?
        """
        user_input = state["user_input"]

        # 鍒嗘瀽鎰忓浘锛堜紶鍏ュ璇濆巻鍙诧級
        conversation_history = state.get("shared_memory", {}).get("conversation_history")
        related_history = state.get("shared_memory", {}).get("related_history")
        analysis = self.analyze_intent(user_input, conversation_history, related_history)

        # 鏋勫缓浠诲姟闃熷垪
        task_queue: List[TaskItem] = []
        for task_data in analysis.get("tasks", []):
            task_queue.append(build_task_item_from_plan(task_data))

        # 鎸変紭鍏堢骇鎺掑簭锛堥珮浼樺厛绾у湪鍓嶏級
        task_queue.sort(key=lambda x: x["priority"], reverse=True)

        # 鏇存柊鐘舵€?
        state["current_intent"] = analysis.get("intent", "unknown")
        state["intent_confidence"] = analysis.get("confidence", 0.0)
        state["task_queue"] = task_queue
        state["needs_human_confirm"] = analysis.get("is_high_risk", False) or any(
            task.get("requires_confirmation", False) for task in task_queue
        )
        state["shared_memory"]["router_high_risk_reason"] = analysis.get("high_risk_reason", "")
        state["execution_status"] = "routing"

        # 娣诲姞绯荤粺娑堟伅鍒?messages
        from langchain_core.messages import SystemMessage
        state["messages"].append(
            SystemMessage(content=f"Router 鍒嗘瀽瀹屾垚: {analysis.get('reasoning', '')}")
        )

        return state

    def create_hackernews_tasks(self) -> List[TaskItem]:
        """
        涓?Hacker News 娴嬭瘯鐢ㄤ緥鍒涘缓棰勫畾涔変换鍔?
        杩欐槸涓€涓究鎹锋柟娉曪紝鐢ㄤ簬娴嬭瘯
        """
        return [
            TaskItem(
                task_id="task_1_scrape",
                task_type="web_worker",
                description="抓取 Hacker News 首页前 5 条新闻的标题和链接",
                params={
                    "url": "https://news.ycombinator.com",
                    "action": "scrape",
                    "selectors": {
                        "items": ".athing",
                        "title": ".titleline > a",
                        "link": ".titleline > a@href",
                    },
                    "limit": 5,
                },
                status="pending",
                result=None,
                priority=10,
            ),
            TaskItem(
                task_id="task_2_save",
                task_type="file_worker",
                description="灏嗘姄鍙栫殑鏂伴椈鏁版嵁淇濆瓨鍒版闈㈢殑 txt 鏂囦欢",
                params={
                    "action": "write",
                    "file_path": "~/Desktop/news_summary.txt",
                    "data_source": "task_1_scrape",  # 渚濊禆涓婁竴涓换鍔＄殑缁撴灉
                    "format": "txt",
                },
                status="pending",
                result=None,
                priority=5,
            ),
        ]
