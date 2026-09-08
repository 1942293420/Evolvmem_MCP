#!/usr/bin/env node
// Build a self-contained architecture atlas from repository evidence and Archify specs.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { gzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const options = {
  '--source': path.join(repo, 'docs/evolvmem-atlas.json'),
  '--template': path.join(repo, 'docs/evolvmem-atlas-template.html'),
  '--out-dir': path.join(os.tmpdir(), 'evolvmem-atlas'),
  '--archify': path.join(os.homedir(), '.agents/skills/archify/bin/archify.mjs'),
};
let validateOnly = false;
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i];
  if (key === '--validate-only') { validateOnly = true; continue; }
  if (!(key in options) || !process.argv[i + 1]) throw new Error('Unknown or incomplete argument: ' + key);
  options[key] = path.resolve(process.argv[++i]);
}
const out = options['--out-dir'];
fs.mkdirSync(out, { recursive: true });
const atlas = JSON.parse(fs.readFileSync(options['--source'], 'utf8'));
const digest = value => createHash('sha256').update(value).digest('hex');
const receipts = [], failures = [], specs = [];
for (const chapter of atlas.chapters) {
  if (!/^[a-z0-9-]+$/.test(chapter.id)) throw new Error('Invalid chapter ID');
  const spec = path.join(out, chapter.id + '.architecture.json');
  fs.writeFileSync(spec, JSON.stringify(chapter.diagram, null, 2) + '\n');
  const result = spawnSync(process.execPath, [options['--archify'], 'validate', 'architecture', spec, '--quality', 'showcase', '--json'], { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
  fs.writeFileSync(path.join(out, chapter.id + '.validation.json'), result.stdout || result.stderr || String(result.error));
  let report;
  try { report = JSON.parse(result.stdout); } catch (_) { report = { stderr: result.stderr, error: String(result.error) }; }
  if (result.status !== 0 || !report.ok || report.checks?.length !== 9 || report.composition?.summary?.errors || report.composition?.summary?.warnings) {
    failures.push({ chapter: chapter.id, report });
  } else {
    specs.push({ chapter, spec });
    console.log(JSON.stringify({ chapter: chapter.id, validation: '9/9', errors: 0, warnings: 0 }));
  }
}
if (failures.length) {
  console.error(JSON.stringify({ ok: false, failures }, null, 2));
  process.exit(1);
}
if (validateOnly) process.exit(0);
for (const { chapter, spec } of specs) {
  const htmlPath = path.join(out, chapter.id + '.html');
  const result = spawnSync(process.execPath, [options['--archify'], 'deliver', 'architecture', spec, htmlPath, '--quality', 'showcase', '--json'], { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
  if (result.status !== 0) throw new Error('Delivery failed for ' + chapter.id + ': ' + result.stdout + result.stderr);
  const receipt = JSON.parse(result.stdout);
  if (!receipt.ok || receipt.validation?.checksPassed !== 9 || receipt.validation?.errors || receipt.validation?.warnings) throw new Error('Incomplete showcase delivery: ' + chapter.id);
  fs.writeFileSync(path.join(out, chapter.id + '.receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
  chapter.html = fs.readFileSync(htmlPath, 'utf8');
  if (digest(chapter.html) !== receipt.artifact.sha256) throw new Error('Delivered artifact changed: ' + chapter.id);
  receipts.push({ chapter: chapter.id, type: 'architecture', specification: receipt.specification, artifact: receipt.artifact, validation: receipt.validation });
}
const payload = gzipSync(Buffer.from(JSON.stringify(atlas)), { level: 9 }).toString('base64');
const template = fs.readFileSync(options['--template'], 'utf8');
if (template.split('__ATLAS_PAYLOAD__').length !== 2) throw new Error('Template must contain exactly one payload placeholder');
const html = template.replace('__ATLAS_PAYLOAD__', payload);
fs.writeFileSync(path.join(out, 'workflow.html'), html);
fs.writeFileSync(path.join(out, 'workflow-diagram.html'), atlas.chapters[0].html);
const receipt = { schema_version: 1, reviewed_at: atlas.reviewed_at, source: { sha256: digest(fs.readFileSync(options['--source'])), bytes: fs.statSync(options['--source']).size }, chapters: receipts, atlas: { sha256: digest(html), bytes: Buffer.byteLength(html) }, visual_review: 'Pending browser inspection; diagram validation does not substitute for visual review.' };
fs.writeFileSync(path.join(out, 'atlas-receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ ok: true, chapters: receipts.length, output: path.join(out, 'workflow.html'), artifact: receipt.atlas }));
