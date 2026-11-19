# EvolvMem 编辑采纳去重修复源码补丁

此分支提供一份用于已有完整源码检出的增量补丁。适用基线和校验值如下。

- 基线提交 `259c65be7bb5e4504112c54aa0a5f0313bbb1b9f`
- 补丁 SHA-256 `d6f1822a58e17415bb8dfe8dc2f77644f0b1b5294bfb88d7a28ed7f56ee9e223`
- 应用后的 Git 树 `421e2ed3ec7a6fc03ea59300f73358c96530a162`

在拥有该基线的干净检出中验证。将 PATCH 设为本目录补丁的绝对路径，再依次执行。

```sh
test "$(git rev-parse HEAD)" = 259c65be7bb5e4504112c54aa0a5f0313bbb1b9f
test -z "$(git status --porcelain)"
printf '%s  %s\n' d6f1822a58e17415bb8dfe8dc2f77644f0b1b5294bfb88d7a28ed7f56ee9e223 "$PATCH" | sha256sum -c -
git apply --check --index --unidiff-zero "$PATCH"
git apply --index --unidiff-zero "$PATCH"
test "$(git write-tree)" = 421e2ed3ec7a6fc03ea59300f73358c96530a162
```

补丁包含本次变更的源码、说明与测试。应用后可在隔离测试环境运行相关回归测试。
