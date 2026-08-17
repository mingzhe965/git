import os
import re
import ast
import json
from volcenginesdkarkruntime import Ark

# 导入你本地的法院字典（court_data.py 文件）
from court_data import COURT_MSYS_DICT

# ====================== 配置区 ======================
# 直接使用模型名称，不需要 endpoint_id
MODEL_NAME = "doubao-seed-2-1-pro-260628"

# 日志文件名（确保文件在同一目录下）
LOG_FILE = "case_log.txt"
# ===================================================

# 初始化豆包客户端（API Key 从环境变量 ARK_API_KEY 读取）
client = Ark(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=os.getenv("ARK_API_KEY"),
)


def extract_dict_after_marker(text, marker):
    """
    从文本中提取 marker 后面的第一个完整字典（支持嵌套）。
    例如：从 "... callback body: {'a': 1, 'b': {'c': 2}}" 中提取出完整字典字符串。
    """
    idx = text.find(marker)
    if idx == -1:
        return None

    # 从 marker 后面开始找第一个 '{'
    start = text.find("{", idx)
    if start == -1:
        return None

    depth = 0
    end = -1
    in_string = False
    string_char = None

    for i in range(start, len(text)):
        char = text[i]

        # 如果当前在字符串内部，跳过括号匹配，直到遇到未转义的结束引号
        if in_string:
            if char == string_char and text[i - 1] != "\\":
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
        return text[start:end + 1]
    return None


def extract_defendants_from_log(log_path):
    """
    从日志文件中提取被告姓名和身份证地址。
    适配格式：文件整体是 JSON 数组，每个元素包含 "line" 字段，
    line 字段内包含 "callback body: {Python字典}"。
    返回：{被告姓名: 身份证地址}
    """
    defendants = {}

    # 1. 读取文件并自动尝试多种编码
    raw_data = None
    try:
        with open(log_path, "rb") as f:
            raw_data = f.read()
    except Exception as e:
        print(f"[错误] 读取日志文件失败：{e}")
        return defendants

    content = None
    # 依次尝试常见中文编码
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb18030"]:
        try:
            content = raw_data.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        print("[错误] 日志文件编码无法识别，请另存为 UTF-8 编码。")
        return defendants

    # 2. 解析外层 JSON 数组
    try:
        log_list = json.loads(content)
    except Exception as e:
        print(f"[错误] 日志文件不是合法的 JSON 数组：{e}")
        return defendants

    if not isinstance(log_list, list):
        print("[错误] 日志文件格式不正确，预期是一个 JSON 数组（[...]）。")
        return defendants

    # 3. 遍历每一条日志
    for item in log_list:
        if not isinstance(item, dict):
            continue

        line_content = item.get("line", "")
        if not line_content:
            continue

        # 4. 提取 callback body 后面的字典字符串
        dict_str = extract_dict_after_marker(line_content, "callback body: ")
        if not dict_str:
            continue

        # 5. 将 Python 字典字符串转换为真正的字典
        try:
            body_data = ast.literal_eval(dict_str)
        except Exception:
            # 日志可能被截断导致解析失败，跳过即可
            continue

        if not isinstance(body_data, dict):
            continue

        # 6. 从字典中提取被告信息
        case_list = body_data.get("caseList", [])
        for case in case_list:
            if not isinstance(case, dict):
                continue

            defendant = case.get("defendant", {})
            if not isinstance(defendant, dict):
                continue

            detail_list = defendant.get("detailList", [])
            if not detail_list:
                continue

            # 兼容二维数组：[[{...}, {...}]]
            fields = []
            if isinstance(detail_list[0], list):
                fields = detail_list[0]
            elif isinstance(detail_list[0], dict):
                fields = detail_list

            name = None
            address = None
            for field in fields:
                if not isinstance(field, dict):
                    continue
                code = field.get("code")
                value = field.get("value", "")
                if code == "bgmc" and isinstance(value, str):
                    name = value.strip()
                elif code == "bgsfzdz" and isinstance(value, str):
                    address = value.strip()

            if name and address and name not in defendants:
                defendants[name] = address

    return defendants


def call_llm(system_prompt, user_prompt):
    """调用豆包 Chat Completions 接口。"""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()


def choose_from_candidates(address, candidates, level_name):
    """
    让模型根据地址，从候选列表中选出最匹配的一项。
    """
    if not candidates:
        return None

    # 候选只有一个时直接返回，不调用 API
    if len(candidates) == 1:
        return candidates[0]

    system_prompt = (
        f"你是一个专业的法律地址解析助手。"
        f"请根据用户给出的地址，判断它属于以下哪个{level_name}。"
        f"必须严格从候选列表中选择，直接输出选项名称，"
        f"不要输出任何解释、引号、标点或多余文字。"
        f"如果无法判断，输出：无法判断"
    )

    user_prompt = f"地址：{address}\n候选{level_name}列表：{candidates}"

    try:
        answer = call_llm(system_prompt, user_prompt)
    except Exception as e:
        print(f"[警告] 模型调用失败：{e}")
        return None

    # 清洗模型返回结果
    answer = answer.strip().strip("“\"'‘").strip()

    if answer in candidates:
        return answer

    # 容错：模型可能带了多余文字，做一次包含匹配
    for item in candidates:
        if item in answer or answer in item:
            return item

    return None


def match_court_by_address(address, court_data):
    """
    根据法院字典结构匹配：
    地址 → 省份/直辖市/自治区 → 市/地区/州 → 具体人民法院
    """
    # 第1轮：选省份
    provinces = list(court_data.keys())
    province = choose_from_candidates(address, provinces, "省份/直辖市/自治区")
    if not province:
        return "未能确定省份"

    # 第2轮：选市/地区
    cities = list(court_data[province].keys())
    city = choose_from_candidates(address, cities, "市/地区/州")
    if not city:
        return f"未能确定{province}下的市/地区"

    # 第3轮：选具体法院
    courts = court_data[province][city]
    if len(courts) == 1:
        return courts[0]

    court = choose_from_candidates(address, courts, "人民法院")
    if court:
        return court

    return "未能确定具体法院"


def main():
    # 检查 API Key
    if not os.getenv("ARK_API_KEY"):
        print("❌ 请先在当前命令行设置环境变量 ARK_API_KEY！")
        print("   例如（CMD）：set ARK_API_KEY=你的真实APIKey")
        return

    # 检查日志文件是否存在
    if not os.path.exists(LOG_FILE):
        print(f"❌ 找不到日志文件：{LOG_FILE}")
        print(f"   请确认文件和本程序在同一个文件夹下。")
        return

    # 1. 从日志提取被告及地址
    print("=" * 60)
    print("正在从日志提取被告信息...")
    defendants = extract_defendants_from_log(LOG_FILE)

    if not defendants:
        print("❌ 没有提取到任何被告信息，请检查日志格式。")
        return

    print(f"✅ 成功提取到 {len(defendants)} 个被告：")
    for name, addr in defendants.items():
        print(f"   {name} -> {addr}")
    print("-" * 60)

    # 2. 逐个匹配管辖法院
    final_result = {}
    for name, address in defendants.items():
        print(f"正在匹配：{name}")
        print(f"  地址：{address}")
        court = match_court_by_address(address, COURT_MSYS_DICT)
        final_result[name] = court
        print(f"  管辖法院：{court}\n")

    # 3. 打印最终结果
    print("=" * 60)
    print("最终结果（被告 -> 管辖法院）：")
    print(json.dumps(final_result, ensure_ascii=False, indent=2))

    # 4. 保存结果到 result.json
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(final_result, f, ensure_ascii=False, indent=2)
    print("=" * 60)
    print("✅ 结果已保存到 result.json")


if __name__ == "__main__":
    main()