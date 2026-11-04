#!/usr/bin/env node
/** my-RAG 一键发版（Docker 镜像发布）
 *
 * 流程：校验分支/工作区 → 升根 package.json 版本 → 提交 → 打 annotated tag → 推送。
 * 镜像本身由 .github/workflows/docker.yml 在 tag 推送后构建并推送到 GHCR，
 * 本脚本不跑 docker build（CI 的缓存和网络更合适）。
 *
 * 用法：
 *   node deploy/release.mjs                     # 交互式
 *   node deploy/release.mjs --patch --yes       # 非交互（给 svn-git 应用调用）
 *   node deploy/release.mjs --minor --no-push   # 只提交 + 打 tag，不推送
 *
 * 选项：
 *   --patch | --minor | --major   直接指定升哪一位
 *   --yes                         跳过确认
 *   --note "一句话说明"            发布说明（写入 commit 和 tag 注释）
 *   --no-push                     不推送、不触发 CI
 *   --branch <名字>               允许在非 main 分支发版（默认拒绝）
 */

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PKG = path.join(ROOT, 'package.json');

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const optVal = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
};

const LEVEL = ['patch', 'minor', 'major'].find((l) => flag(`--${l}`)) ?? null;
const YES = flag('--yes');
const NOTE = optVal('--note')?.trim() ?? '';
const NO_PUSH = flag('--no-push');
const ALLOW_BRANCH = optVal('--branch') ?? 'main';

function sh(cmd, args, { allowFail = false } = {}) {
  try {
    return execFileSync(cmd, args, {
      cwd: ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch (e) {
    if (allowFail) return '';
    const detail = (e.stderr || e.stdout || e.message || '').toString().trim();
    throw new Error(`执行失败：${cmd} ${args.join(' ')}\n${detail}`);
  }
}
const git = (args, opts) => sh('git', args, opts);

function readVersion() {
  const pkg = JSON.parse(fs.readFileSync(PKG, 'utf8'));
  const v = String(pkg.version ?? '');
  if (!/^\d+\.\d+\.\d+$/.test(v)) throw new Error(`package.json 的 version 不合法：${v || '（空）'}`);
  return v;
}

function bump(cur, level) {
  const [a = 0, b = 0, c = 0] = cur.split('.').map((n) => Number(n) || 0);
  if (level === 'major') return `${a + 1}.0.0`;
  if (level === 'minor') return `${a}.${b + 1}.0`;
  return `${a}.${b}.${c + 1}`;
}

function lastTag() {
  return git(['describe', '--tags', '--abbrev=0'], { allowFail: true });
}

function defaultNote(tag) {
  const last = lastTag();
  const args = ['log', '-1', '--format=%s'];
  if (last) args.push(`${last}..HEAD`);
  return (git(args, { allowFail: true }) || `${tag} 发布`).slice(0, 60);
}

async function main() {
  if (!fs.existsSync(path.join(ROOT, '.git'))) throw new Error('项目根不是 git 仓库');

  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  if (branch !== ALLOW_BRANCH) {
    throw new Error(`当前分支是 ${branch}，只允许在 ${ALLOW_BRANCH} 分支发版（可用 --branch 指定）`);
  }

  // 未跟踪文件不影响发版（应用里的 package.json / release.mjs 首次提交时也走这条路）
  const dirty = git(['status', '--porcelain', '--untracked-files=no']);
  if (dirty) throw new Error(`工作区有未提交改动，请先提交或还原：\n${dirty}`);

  const cur = readVersion();
  let level = LEVEL;
  let note = NOTE;

  if (!level) {
    if (!process.stdin.isTTY) throw new Error('非交互模式必须指定 --patch / --minor / --major');
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      const ans = (await rl.question(`当前版本 ${cur}，升哪一位？(patch/minor/major，默认 patch) `))
        .trim()
        .toLowerCase();
      level = ['patch', 'minor', 'major'].includes(ans) ? ans : 'patch';
      if (!note) note = (await rl.question('发布说明（回车用默认）：')).trim();
    } finally {
      rl.close();
    }
  }
  if (!['patch', 'minor', 'major'].includes(level)) throw new Error(`未知版本级别：${level}`);

  const next = bump(cur, level);
  const tag = `v${next}`;
  if (!note) note = defaultNote(tag);
  if (!note) throw new Error('发布说明不能为空');
  if (/[\r\n]/.test(note)) throw new Error('发布说明不能包含换行');

  if (git(['tag', '-l', tag])) throw new Error(`本地已存在 tag：${tag}`);
  if (git(['ls-remote', '--tags', 'origin', tag], { allowFail: true })) {
    throw new Error(`远端已存在 tag：${tag}`);
  }

  if (!YES && process.stdin.isTTY) {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      const ok = (await rl.question(`将发布 ${tag}：${note}\n继续？(y/N) `)).trim().toLowerCase();
      if (ok !== 'y' && ok !== 'yes') {
        console.log('已取消');
        return;
      }
    } finally {
      rl.close();
    }
  }

  // 只改根 package.json 的 version，保留原文件格式
  const raw = fs.readFileSync(PKG, 'utf8');
  const updated = raw.replace(/"version"\s*:\s*"[^"]*"/, `"version": "${next}"`);
  if (updated === raw) throw new Error('package.json 里没有找到顶层 version 字段');
  fs.writeFileSync(PKG, updated);

  git(['add', 'package.json']);
  git(['commit', '-m', `${tag}：${note}`]);
  git(['tag', '-a', tag, '-m', `${tag}：${note}`]);
  console.log(`✔ ${tag} 已提交并打标签`);

  if (NO_PUSH) {
    console.log('已跳过推送（--no-push）');
    return;
  }

  git(['push', '--follow-tags']);
  console.log('✔ 已推送到 origin');
  console.log(`  GitHub Actions 将构建并推送镜像：ghcr.io/<owner>/<repo>-{backend,frontend,ragas}:${tag}`);
}

main().catch((e) => {
  console.error(`✖ ${e.message}`);
  process.exit(1);
});
