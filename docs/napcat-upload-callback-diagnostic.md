# NapCat Group Upload Callback Diagnostic

面向维护者的专项诊断：区分 NapCat `v4.18.13` 群文件上传完成回调的两种失败模式。补丁只加警告日志，不改变上传行为或成功判定。日志含 QQ/群号、消息 ID、文件名等敏感信息，复现结束后立刻恢复原 bundle。完整步骤见下文英文说明。

This procedure distinguishes two failure modes in NapCat `v4.18.13`:

1. QQNT never emits a final successful `onMsgInfoListUpdate` record.
2. QQNT emits one, but its `guildId` or `sendStatus` does not satisfy NapCat's correlation predicate.

The diagnostic patch only adds warning logs. It does not change upload behavior or success criteria. The logs contain
QQ/group identifiers, message IDs, filenames, file UUIDs, and sizes; treat them as sensitive and restore the original
bundle immediately after the controlled reproduction.

## Prepare

Build from the exact deployed tag so unrelated NapCat changes do not affect the result. Run these commands from the
adapter repository root:

```bash
git clone --branch v4.18.13 --depth 1 https://github.com/NapNeko/NapCatQQ.git /tmp/NapCatQQ-v4.18.13
git -C /tmp/NapCatQQ-v4.18.13 apply \
  "$PWD/scripts/napcat-v4.18.13-upload-callback-diagnostic.patch"
cd /tmp/NapCatQQ-v4.18.13
corepack pnpm install --frozen-lockfile
corepack pnpm build:webui
NAPCAT_VERSION=4.18.13 corepack pnpm build:shell
```

With pnpm 11, run `corepack pnpm approve-builds --all` and repeat installation if pnpm initially reports
`ERR_PNPM_IGNORED_BUILDS` in this disposable source checkout.

The diagnostic bundle is expected at:

```text
/tmp/NapCatQQ-v4.18.13/packages/napcat-shell/dist/napcat.mjs
```

Before deployment, confirm that the bundle contains `UploadCallbackDiag`.

## Deploy Later

Deployment requires a brief NapCat restart. Do not run these commands during preparation:

```bash
docker cp napcat:/app/napcat/napcat.mjs /tmp/napcat.mjs.v4.18.13.backup
docker cp /tmp/NapCatQQ-v4.18.13/packages/napcat-shell/dist/napcat.mjs napcat:/app/napcat/napcat.mjs
docker restart napcat
```

The replacement survives `docker restart`, but not container recreation or image upgrade.

## Reproduce

Upload one uniquely named small file through `onebot_upload_file`. Avoid concurrent file uploads during the test.
Use a quiet test window because the patch records compact metadata for every `onMsgInfoListUpdate` callback while the
diagnostic upload is pending.

Capture only diagnostic lines:

```bash
docker logs --since 10m napcat 2>&1 | rg 'UploadCallbackDiag'
```

Interpretation:

- `start` and `service`, but no `update`: QQNT emitted no relevant file update.
- `update` exists, but `guildId` differs from `expectedCorrelation`: NapCat's correlation key was lost or replaced.
- `guildId` matches, but `sendStatus` never reaches the success value: QQNT never reported final success.
- `matched` appears promptly, but the OneBot response is still delayed: investigate the WebSocket action response path instead.

Keep the complete diagnostic lines, the upload start time, group ID, requested filename, resulting group message sequence, and whether the file actually appeared.

## Restore

```bash
docker cp /tmp/napcat.mjs.v4.18.13.backup napcat:/app/napcat/napcat.mjs
docker restart napcat
```

Remove `/tmp/NapCatQQ-v4.18.13` after the diagnosis is complete.
