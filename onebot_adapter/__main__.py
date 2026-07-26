"""Entry point: ``python -m onebot_adapter``."""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sys

from onebot_adapter import __version__
from onebot_adapter.app import run


def _manage_automation_api(args) -> int:
    from onebot_adapter.config import load_config, save_config

    cfg = load_config()
    changes = {}
    raw_key: str | None = None
    if args.generate_api_key:
        if cfg.automation_api_key_hash:
            print("✗ API key 已存在；请使用 --rotate-api-key")
            return 1
        raw_key = "hoa_" + secrets.token_urlsafe(32)
        changes["automation_api_key_hash"] = hashlib.sha256(raw_key.encode()).hexdigest()
    elif args.rotate_api_key:
        raw_key = "hoa_" + secrets.token_urlsafe(32)
        changes["automation_api_key_hash"] = hashlib.sha256(raw_key.encode()).hexdigest()
    elif args.revoke_api_key:
        changes.update(automation_api_key_hash="", automation_api_enabled=False)
    if args.enable_api:
        if not (changes.get("automation_api_key_hash") or cfg.automation_api_key_hash):
            print("✗ 尚未配置 API key；请先使用 --generate-api-key")
            return 1
        changes["automation_api_enabled"] = True
    if args.disable_api:
        changes["automation_api_enabled"] = False
    new_cfg = cfg.with_overrides(**changes)
    save_config(
        new_cfg, source="cli", reason="cli.automation_api_management",
        actor="command_line", submitted_fields=sorted(changes),
    )
    if raw_key:
        print("✓ 自动化 API key（仅显示一次）:")
        print(raw_key)
    print(f"  自动化 API: {'已启用' if new_cfg.automation_api_enabled else '已关闭'}")
    return 0


def _init_config(force: bool) -> int:
    from onebot_adapter.config import (
        AdapterConfig,
        config_path,
        ensure_tokens,
        load_config,
        save_config,
    )

    target = config_path()
    if target.exists() and not force:
        print(f"✗ 配置文件已存在: {target}")
        print("  使用 --init-config --force 覆盖(保留已有 token)")
        return 1

    existing_tokens: dict[str, str] = {}
    if target.exists():
        old = load_config(target)
        existing_tokens = {
            "onebot_ws_token": old.onebot_ws_token,
            "hermes_ws_token": old.hermes_ws_token,
            "webui_token": old.webui_token,
            "automation_api_key_hash": old.automation_api_key_hash,
        }

    cfg = AdapterConfig(**existing_tokens)
    tokens_were_missing = not all(
        existing_tokens[key] for key in ("onebot_ws_token", "hermes_ws_token", "webui_token")
    )
    reason = "cli.force_reinitialize" if target.exists() and force else "cli.init_config"
    cfg = ensure_tokens(
        cfg,
        source="cli",
        reason=reason,
        actor="command_line",
    )
    # ensure_tokens persists only when at least one token is missing. A forced
    # reinitialization with three existing tokens still needs to write the
    # default configuration while preserving those tokens.
    if not tokens_were_missing:
        save_config(
            cfg,
            source="cli",
            reason=reason,
            actor="command_line",
            submitted_fields=sorted(cfg.to_dict()),
        )
    print(f"✓ 配置文件已生成: {target}")
    print(f"  onebot_ws_token:  {cfg.onebot_ws_token}")
    print(f"  hermes_ws_token:  {cfg.hermes_ws_token}")
    print(f"  webui_token:      {cfg.webui_token}")
    print("  编辑完成后运行 hermes-onebot-adapter 启动服务")
    print("  首次登录 WebUI 时输入 webui_token")
    return 0


def _install(args) -> int:
    from onebot_adapter import installer
    from onebot_adapter.config import load_config

    adapter_url = args.adapter_url
    adapter_token = args.adapter_token

    if adapter_url is None or adapter_token is None:
        cfg = load_config()
        if adapter_url is None:
            adapter_url = f"ws://127.0.0.1:{cfg.hermes_ws_port}{cfg.hermes_ws_path}"
        if adapter_token is None:
            adapter_token = cfg.hermes_ws_token

    result = installer.install(
        args.hermes_dir,
        adapter_url=adapter_url,
        adapter_token=adapter_token,
    )

    if result.get("error"):
        print(f"✗ 安装失败: {result['error']}")
        return 1

    print(f"✓ 插件已安装到 {result['plugin_dest']}")
    print(f"  Hermes 目录: {result['hermes_dir']}")
    print(f"  复制文件: {', '.join(result.get('copied', []))}")
    if result.get("env_vars"):
        print(f"  环境变量已写入 .env: {', '.join(result['env_vars'].keys())}")
    print(f"  {result.get('note', '')}")
    return 0


def _uninstall(args) -> int:
    from onebot_adapter import installer

    result = installer.uninstall(args.hermes_dir)

    if result.get("error"):
        print(f"✗ 卸载失败: {result['error']}")
        return 1

    print(f"✓ 插件已从 {result['plugin_dest']} 移除")
    print(f"  Hermes 目录: {result['hermes_dir']}")
    if result.get("env_cleaned"):
        print("  环境变量已从 .env 清理")
    print(f"  {result.get('note', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-onebot-adapter", description="Hermes OneBot Adapter service")
    parser.add_argument("--host", default="127.0.0.1", help="WebUI/API bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="WebUI/API port (default: from config, 18820)")
    parser.add_argument("--no-webui", action="store_true", help="不启动 WebUI 管理界面")
    parser.add_argument("--version", action="version", version=f"hermes-onebot-adapter {__version__}")
    key_group = parser.add_mutually_exclusive_group()
    key_group.add_argument("--generate-api-key", action="store_true", help="生成自动化 API key 后退出")
    key_group.add_argument("--rotate-api-key", action="store_true", help="轮换自动化 API key 后退出")
    key_group.add_argument("--revoke-api-key", action="store_true", help="撤销自动化 API key 后退出")
    api_group = parser.add_mutually_exclusive_group()
    api_group.add_argument("--enable-api", action="store_true", help="启用自动化工具 API")
    api_group.add_argument("--disable-api", action="store_true", help="关闭自动化工具 API")
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="生成默认配置文件后退出(不启动服务)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="配合 --init-config 强制覆盖已有配置(保留已有 token,其余字段重置为默认)",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    install_parser = subparsers.add_parser("install", help="安装 OneBot 插件到 Hermes")
    install_parser.add_argument("--hermes-dir", default=None, help="Hermes 安装目录 (默认: $HERMES_HOME 或 ~/.hermes)")
    install_parser.add_argument("--adapter-url", default=None, help="适配器 WS 地址 (默认: 从 config.json 读取)")
    install_parser.add_argument("--adapter-token", default=None, help="适配器 WS token (默认: 从 config.json 读取)")

    uninstall_parser = subparsers.add_parser("uninstall", help="从 Hermes 卸载 OneBot 插件")
    uninstall_parser.add_argument(
        "--hermes-dir", default=None, help="Hermes 安装目录 (默认: $HERMES_HOME 或 ~/.hermes)",
    )

    args = parser.parse_args(argv)

    if any((
        args.generate_api_key, args.rotate_api_key, args.revoke_api_key,
        args.enable_api, args.disable_api,
    )):
        return _manage_automation_api(args)

    if args.command == "install":
        return _install(args)
    if args.command == "uninstall":
        return _uninstall(args)

    if args.init_config:
        return _init_config(force=args.force)

    run(host=args.host, port=args.port, no_webui=args.no_webui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
