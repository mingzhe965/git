import os
import sys

print("=" * 50)
print("1. 检查 Python 版本：")
print(sys.version)

print("=" * 50)
print("2. 检查 ARK_API_KEY 环境变量：")
api_key = os.getenv("ARK_API_KEY")
if api_key:
    print(f"   已读取到 API Key，长度：{len(api_key)}")
else:
    print("   ❌ 没有读取到 ARK_API_KEY！")

print("=" * 50)
print("3. 检查 court_data 能否导入：")
try:
    from court_data import COURT_MSYS_DICT
    print(f"   ✅ 导入成功，法院字典共有 {len(COURT_MSYS_DICT)} 个省级地区")
except Exception as e:
    print(f"   ❌ 导入失败：{e}")

print("=" * 50)
print("4. 检查 case_log.txt 能否读取：")
try:
    import json
    with open("case_log.txt", "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"   ✅ 读取成功，日志共有 {len(data)} 条记录")
except Exception as e:
    print(f"   ❌ 读取失败：{e}")

print("=" * 50)
print("5. 检查 extract_defendants_from_log 能否提取到被告：")
try:
    from court_finder import extract_defendants_from_log
    defendants = extract_defendants_from_log("case_log.txt")
    print(f"   提取到 {len(defendants)} 个被告：")
    for name, addr in defendants.items():
        print(f"     - {name} : {addr}")
except Exception as e:
    print(f"   ❌ 提取失败：{e}")
    import traceback
    traceback.print_exc()

print("=" * 50)
print("测试完成")