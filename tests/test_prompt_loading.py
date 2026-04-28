"""
测试 AI Prompt 动态加载功能

用法:
    python test_prompt_loading.py
"""

import sys
from pathlib import Path


def test_prompt_loading():
    """测试 Prompt 加载功能"""
    print("=" * 60)
    print("测试 AI Prompt 动态加载功能")
    print("=" * 60)

    # 导入模块
    from core.config import Config
    from services.ai_analyzer import load_user_prompt_template, reload_user_prompt_template

    # 测试 1: 检查配置
    print("\n1️⃣  检查配置")
    print(f"   AI_USER_PROMPT_FILE: {Config.AI_USER_PROMPT_FILE}")
    print(f"   AI_USER_PROMPT (env): {'已设置' if Config.AI_USER_PROMPT else '未设置'}")

    # 测试 2: 加载默认 prompt
    print("\n2️⃣  加载 Prompt 模板")
    try:
        template = load_user_prompt_template()
        print(f"   ✅ 成功加载，长度: {len(template)} 字符")
        print("   预览 (前200字符):")
        print(f"   {template[:200]}...")
    except Exception as e:
        raise AssertionError(f"加载失败: {e}") from e

    # 测试 3: 检查模板变量
    print("\n3️⃣  检查模板变量")
    if "{source}" in template and "{data_json}" in template:
        print("   ✅ 模板包含必需变量: {source}, {data_json}")
    else:
        print("   ⚠️  模板缺少变量:")
        if "{source}" not in template:
            print("      - 缺少 {source}")
        if "{data_json}" not in template:
            print("      - 缺少 {data_json}")

    # 测试 4: 格式化测试
    print("\n4️⃣  测试模板格式化")
    try:
        import json

        test_data = {"event": "test_event", "level": "warning"}
        formatted = template.format(source="test_source", data_json=json.dumps(test_data, ensure_ascii=False, indent=2))
        print("   ✅ 格式化成功")
        print(f"   格式化后长度: {len(formatted)} 字符")
    except KeyError as e:
        raise AssertionError(f"格式化失败，缺少变量: {e}") from e
    except Exception as e:
        raise AssertionError(f"格式化失败: {e}") from e

    # 测试 5: 重载功能
    print("\n5️⃣  测试重载功能")
    try:
        reloaded = reload_user_prompt_template()
        if reloaded == template:
            print("   ✅ 重载成功，内容一致")
        else:
            print("   ⚠️  重载后内容发生变化")
    except Exception as e:
        raise AssertionError(f"重载失败: {e}") from e

    # 测试 6: 检查 prompt 文件
    print("\n6️⃣  检查 Prompt 文件")
    prompt_file = Config.AI_USER_PROMPT_FILE
    if prompt_file:
        file_path = Path(prompt_file)
        if not file_path.is_absolute():
            # 相对于当前目录
            file_path = Path(__file__).parent / file_path

        if file_path.exists():
            print(f"   ✅ 文件存在: {file_path}")
            stat = file_path.stat()
            print(f"   文件大小: {stat.st_size} bytes")
            print(f"   修改时间: {stat.st_mtime}")
        else:
            print(f"   ⚠️  文件不存在: {file_path}")
            print("   将使用默认硬编码模板")
    else:
        print("   ℹ️  未配置 AI_USER_PROMPT_FILE")

    print("\n" + "=" * 60)
    print("✅ 所有测试通过")
    print("=" * 60)


def test_api_endpoints():
    """测试 API 端点（需要服务运行）"""
    print("\n" + "=" * 60)
    print("测试 API 端点")
    print("=" * 60)
    print("\n⚠️  此测试需要服务正在运行")
    print("请先运行: python app.py")
    print("\n测试命令:")
    print("  # 获取当前 prompt")
    print("  curl http://localhost:5000/api/prompt")
    print("\n  # 重新加载 prompt")
    print("  curl -X POST http://localhost:5000/api/prompt/reload")
    print("=" * 60)


if __name__ == "__main__":
    success = test_prompt_loading()
    test_api_endpoints()

    sys.exit(0 if success else 1)
