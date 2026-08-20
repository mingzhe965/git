import os
import re
import ast
import json
import time
from volcenginesdkarkruntime import Ark

# 导入法院字典
from court_data import COURT_MSYS_DICT

# ====================== 配置 ======================
MODEL_NAME = "doubao-seed-2-1-pro-260628"
LOG_FILE = "case_log.txt"
RESULT_FILE = "result.json"
FAIL_FILE = "fail_address.json"
REQUEST_SLEEP = 1.0  # 接口请求间隔，防止限流
# =================================================

client = None


def extract_district_keywords(clean_addr: str):
    patterns = [
        re.compile(r"([^，。,、，]{1,8}区)"),
        re.compile(r"([^，。,、，]{1,8}县)"),
        re.compile(r"([^，。,、，]{1,8}市)"),
    ]
    keywords = set()
    for pat in patterns:
        matches = pat.findall(clean_addr)
        for m in matches:
            if len(m) >=2:
                keywords.add(m)
    return list(keywords)


def local_keyword_match(keywords: list, court_list: list):
    """本地关键词匹配，过滤中级、高级法院，只匹配基层人民法院"""
    if not keywords or not court_list:
        return None
    for kw in keywords:
        for court_name in court_list:
            # 跳过高级、中级法院，只匹配基层区县法院
            if "高级人民法院" in court_name or "中级人民法院" in court_name:
                continue
            if kw in court_name:
                return court_name
    return None

def clean_raw_address(raw_addr: str) -> str:
    """
    地址清洗，去除门牌号、幢号等干扰信息
    """
    if not raw_addr:
        return ""
    addr = raw_addr.strip()
    addr = re.sub(r"湖北省省直辖县级行政区划", "湖北省", addr)
    addr = re.sub(r"\d+\s*号.*", "", addr)
    addr = re.sub(r"\d+\s*幢.*", "", addr)
    addr = re.sub(r"\d+\s*栋.*", "", addr)
    addr = re.sub(r"\d+-\d+.*", "", addr)
    addr = re.sub(r"\d+\s*室.*", "", addr)
    addr = re.sub(r"\d+\s*单元.*", "", addr)
    return addr.strip()


def special_rule_preprocess(clean_addr: str):
    """
    特殊地址标记：直辖市、省直辖县级市
    返回 (is_special, province_name)
    """
    if "重庆市" in clean_addr:
        return True, "重庆市"
    if "北京市" in clean_addr:
        return True, "北京市"
    if "上海市" in clean_addr:
        return True, "上海市"
    if "天津市" in clean_addr:
        return True, "天津市"
    if any(k in clean_addr for k in ["潜江市", "仙桃市", "天门市"]):
        return True, "湖北省"
    return False, None


def extract_dict_after_marker(text, marker):
    idx = text.find(marker)
    if idx == -1:
        return None
    start = text.find("{", idx)
    if start == -1:
        return None
    depth = 0
    end = -1
    in_string = False
    string_char = None
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if char == string_char and text[i-1] != "\\":
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            string_char = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end != -1:
        return text[start:end+1]
    return None


def extract_defendants_from_log(log_path):
    """解析日志，提取法人名称、地址，兼容外层json数组，内部callback body是python单引号字典"""
    defendants = {}
    try:
        with open(log_path, "rb") as f:
            raw_bytes = f.read()
    except Exception as e:
        print(f"[读文件失败] {e}")
        return defendants

    content = None
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
        try:
            content = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        print("[错误] 文件编码无法解析")
        return defendants

    # 外层整体是json数组，允许解析失败，失败就按换行分割逐行尝试
    raw_items = []
    try:
        raw_items = json.loads(content)
    except Exception:
        print("[提示]整体文件不是完整JSON数组，按换行切分逐行解析")
        lines = content.splitlines()
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
                raw_items.append(obj)
            except Exception:
                continue

    if not isinstance(raw_items, list):
        print("[警告]没有拿到日志条目")
        return defendants

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        line_text = item.get("line", "")
        if not line_text or "callback body:" not in line_text:
            continue
        fragment = extract_dict_after_marker(line_text, "callback body: ")
        if not fragment:
            continue
        try:
            # 这里用ast.literal_eval解析单引号python字典，不能json.loads
            body_data = ast.literal_eval(fragment)
        except Exception as e:
            print(f"[单条callback body解析跳过] {str(e)[:120]}")
            continue
        if not isinstance(body_data, dict):
            continue
        case_list = body_data.get("caseList", [])
        for case in case_list:
            if not isinstance(case, dict):
                continue
            def_info = case.get("defendant", {})
            detail_list = def_info.get("detailList", [])
            if not detail_list:
                continue
            fields = detail_list[0] if isinstance(detail_list[0], list) else detail_list
            name = None
            addr = None
            for f in fields:
                if not isinstance(f, dict):
                    continue
                code = f.get("code")
                val = f.get("value", "")
                if code == "bgmc":
                    name = val.strip()
                elif code == "bgsfzdz":
                    addr = val.strip()
            if name and addr and name not in defendants:
                defendants[name] = addr
    return defendants

def call_llm(system_prompt: str, user_prompt: str):
    """调用大模型，异常捕获返回None"""
    global client
    if client is None:
        print("[错误]SDK客户端未初始化")
        return None
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[LLM调用异常] {str(e)}")
        return None


def choose_from_candidates(address: str, candidates: list, level_name: str):
    """从候选列表选择，严格只能输出列表内存在项"""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    sys_prompt = """你是地址匹配专家。
任务：根据输入地址，从候选列表选出唯一匹配的{level_name}。
硬性规则：
1. **只能输出候选列表内完整存在的条目，禁止简写、编造、修改名称**
2. 根据地址中的区县关键词匹配，优先匹配包含区县关键词的选项
3. 完全无法确定时，直接输出：无法判断
只输出结果，不要解释，不要引号。"""
    sys_prompt = sys_prompt.format(level_name=level_name)

    usr_prompt = f"地址：{address}\n候选列表：{candidates}"
    ans = call_llm(sys_prompt, usr_prompt)
    if ans is None:
        return None
    ans = ans.strip().strip("\"'“”‘’")
    if ans in candidates:
        return ans
    # 兜底模糊匹配
    for item in candidates:
        if item in ans or ans in item:
            return item
    return None


def match_court_by_address(raw_address: str, court_data: dict):
    clean_addr = clean_raw_address(raw_address)
    if not clean_addr:
        return "地址为空"

    is_special, spec_prov = special_rule_preprocess(clean_addr)
    prov = None

    if is_special:
        prov = spec_prov
    else:
        province_list = list(court_data.keys())
        prov = choose_from_candidates(clean_addr, province_list, "省份")
        if not prov:
            return "未能识别省份"

    try:
        city_dict = court_data[prov]
    except KeyError:
        return f"[{prov}]省份字典不存在"

    # 特殊地址：跳过地市，拿到该省全部法院列表
    if is_special:
        all_courts = []
        for v in city_dict.values():
            all_courts.extend(v)
        # --------本地关键词优先匹配--------
        kw_list = extract_district_keywords(clean_addr)
        local_hit = local_keyword_match(kw_list, all_courts)
        if local_hit:
            return local_hit
        # 本地没命中，才走大模型兜底
        final_court = choose_from_candidates(clean_addr, all_courts, "人民法院")
        if final_court:
            return final_court
        return "未能匹配对应法院"

    # ----------------普通省份逻辑----------------
    city_list = list(city_dict.keys())
    city = choose_from_candidates(clean_addr, city_list, "城市/地级行政区")
    if not city:
        return f"{prov}：未能识别地市"

    try:
        court_list = city_dict[city]
    except KeyError:
        return f"{prov}-{city}地市字典不存在"

    if len(court_list) == 1:
        return court_list[0]

    # 普通省份同样优先本地关键词匹配
    kw_list = extract_district_keywords(clean_addr)
    local_hit = local_keyword_match(kw_list, court_list)
    if local_hit:
        return local_hit

    # 本地未命中，调用大模型
    final_court = choose_from_candidates(clean_addr, court_list, "人民法院")
    if final_court:
        return final_court
    return "未能匹配对应法院"


def main():
    global client
    ark_key = os.getenv("ARK_API_KEY")
    if not ark_key:
        print("===== 环境变量缺失 =====")
        print('PowerShell执行：$env:ARK_API_KEY="你的火山方舟密钥"')
        return

    client = Ark(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=ark_key
    )

    # ==========单元测试保留==========
    print("######## 单元测试（问题地址）########")
    t1 = "重庆市江北区海尔路 179 号 11 幢 20‑4"
    r1 = match_court_by_address(t1, COURT_MSYS_DICT)
    print(f"测试1｜{t1}")
    print(f"结果｜{r1}\n")

    t2 = "湖北省省直辖县级行政区划潜江市园林街道章华中路72号"
    r2 = match_court_by_address(t2, COURT_MSYS_DICT)
    print(f"测试2｜{t2}")
    print(f"结果｜{r2}\n")
    print("######## 单元测试结束，开始正式日志遍历 ########\n")

    if not os.path.exists(LOG_FILE):
        print(f"[错误] 找不到日志文件 {LOG_FILE}")
        return

    print("===== 读取日志文件 =====")
    name_addr_map = extract_defendants_from_log(LOG_FILE)
    total_cnt = len(name_addr_map)
    if total_cnt == 0:
        print("[警告] 未解析到法人地址数据，请检查case_log.txt")
        return
    print(f"解析成功，共 {total_cnt} 条法人记录\n")

    result_out = {}
    fail_list = []
    success_cnt = 0
    fail_cnt = 0

    idx = 0
    for legal_name, addr in name_addr_map.items():
        idx += 1
        print(f"[{idx}/{total_cnt}] 法人：{legal_name}")
        print(f"原始地址：{addr}")
        res_court = match_court_by_address(addr, COURT_MSYS_DICT)
        result_out[legal_name] = res_court
        print(f"匹配法院：{res_court}")

        if res_court in ("未能匹配对应法院", "未能识别省份", "地址为空") or "不存在" in res_court:
            fail_cnt += 1
            fail_list.append({
                "法人": legal_name,
                "原始地址": addr,
                "返回信息": res_court
            })
        else:
            success_cnt += 1
        print("")
        time.sleep(REQUEST_SLEEP)

    # 输出主结果
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(result_out, f, ensure_ascii=False, indent=2)

    # 输出失败清单
    with open(FAIL_FILE, "w", encoding="utf-8") as f:
        json.dump(fail_list, f, ensure_ascii=False, indent=2)

    print("==================== 统计汇总 ====================")
    print(f"总条数：{total_cnt}")
    print(f"✅匹配成功：{success_cnt}")
    print(f"❌匹配失败：{fail_cnt}")
    print(f"完整结果文件：{RESULT_FILE}")
    print(f"失败清单文件：{FAIL_FILE}")
    print("==================================================")


if __name__ == "__main__":
    main()