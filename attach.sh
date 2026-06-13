#!/bin/bash

# 检查 claude-auto-continue 是否安装
if ! command -v clac &> /dev/null; then
    echo "Installing claude-auto-continue..."
    npm install -g claude-auto-continue
fi

# 检查 claudekeeper 是否安装
if ! command -v claudekeeper &> /dev/null; then
    echo "Installing claudekeeper..."
    npm install -g @antonisoaho/claudekeeper
    claudekeeper install
fi

# 设置默认权限模式为 auto
mkdir -p ~/.claude
if ! grep -q '"defaultMode": "auto"' ~/.claude/settings.json 2>/dev/null; then
    echo '{"permissions": {"defaultMode": "auto"}}' > ~/.claude/settings.json
fi

# 启动无人值守模式
echo "Starting Claude Code in unattended mode..."
echo " - Auto Mode: enabled (automatic permission decisions)"
echo " - Auto-continue: enabled (handles quota limits)"
echo " - Session rotation: enabled (maintains low token usage)"

# 使用 claudekeeper run 包装，它会自动处理会话轮换
claudekeeper run
