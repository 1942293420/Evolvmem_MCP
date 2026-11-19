# EvolvMem 审阅去重修复源码补丁

此分支提供一份用于已有完整源码检出的增量补丁。适用基线和校验值如下。

- 基线提交 `0586e7adf6492fdf34d532fe041c2fab7e9f93da`
- 补丁 SHA-256 `42e1b7c0e7f07a55fb0455ea5255f6f2b65f63b2a4c000716403553e8ed1fdf6`
- 应用后的 Git 树 `ea7965a982ff4c47293b89d6ba9c7dbef997d67e`

在拥有该基线的干净检出中验证。将 PATCH 设为本目录补丁的绝对路径，再依次执行。

```sh
test "$(git rev-parse HEAD)" = 0586e7adf6492fdf34d532fe041c2fab7e9f93da
test -z "$(git status --porcelain)"
printf '%s  %s\n' 42e1b7c0e7f07a55fb0455ea5255f6f2b65f63b2a4c000716403553e8ed1fdf6 "$PATCH" | sha256sum -c -
git apply --check --index --unidiff-zero "$PATCH"
git apply --index --unidiff-zero "$PATCH"
test "$(git write-tree)" = ea7965a982ff4c47293b89d6ba9c7dbef997d67e
```

补丁包含本次变更的源码、说明与测试。应用后可在隔离测试环境运行相关回归测试。
