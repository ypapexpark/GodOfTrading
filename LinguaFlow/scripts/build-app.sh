#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_dir=${script_dir:h}
configuration=${1:-release}
app_dir="$project_dir/build/LinguaFlow.app"
contents_dir="$app_dir/Contents"

cd "$project_dir"
swift build -c "$configuration"

mkdir -p "$contents_dir/MacOS" "$contents_dir/Resources"
cp "$project_dir/.build/$configuration/LinguaFlow" "$contents_dir/MacOS/LinguaFlow"
cp "$project_dir/Resources/Info.plist" "$contents_dir/Info.plist"
codesign --force --deep --sign - "$app_dir"

echo "$app_dir"
