#!/usr/bin/env node
/**
 * Proof that okf-check.mjs can fail — one case per rule.
 *
 *   node scripts/okf-check.test.mjs
 *
 * A gate that has never failed has never been tested, and a checker whose rule silently
 * stopped firing looks identical to a clean bundle from the outside. This builds a
 * minimal conformant bundle in a temp directory, asserts it passes, then mutates one
 * thing at a time and asserts the expected rule fires. Run it whenever the checker
 * changes; it needs no fixtures on disk and leaves nothing behind.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const CHECKER = path.resolve(import.meta.dirname, 'okf-check.mjs');

/** The minimal bundle that must pass every rule. Mutations are applied on top. */
const CLEAN = {
  'okf/index.md': '---\nokf_version: "0.1"\n---\n\n# Bundle\n\n* [service](/service.md)\n',
  'okf/log.md': '# Log\n',
  'okf/service.md':
    '---\ntype: Service\ntitle: Fixture\ndescription: One line.\nresource: https://example.invalid\n---\n\n# Fixture\n',
  'okf/capabilities/index.md': '# Capabilities\n',
  'okf/capabilities/demo.md':
    '---\ntype: Capability\ntitle: Demo\ndescription: One line.\n---\n\n' +
    '#### Scenario: An unreachable source is refused rather than rendered empty\n' +
    '- GIVEN the source is unreachable\n- THEN the response is a 503\n\n' +
    '**Checked by:** demo-refuses-when-unreachable\n',
  'okf/qa/index.md': '# QA\n',
  'okf/qa/demo.md':
    '---\ntype: QA Checklist\ntitle: Demo — checks\ndescription: One line.\n---\n\n' +
    '### Check: demo-refuses-when-unreachable\n' +
    '**Requirement:** An unreachable source is refused rather than rendered empty\n' +
    '**Surface:** Public route\n**Automated:** Manual\n\n' +
    '**Do**\n- Stop the source and request the page\n\n' +
    '**Expect**\n- A 503 with a Retry-After, and no empty body served\n',
  'okf/briefs/index.md': '# Briefs\n',
  'okf/briefs/a-thing.md':
    '---\ntype: Brief\ntitle: A thing\ndescription: One line.\ncapability: demo\n---\n\n' +
    '## Narrative\nWhat happens today, and why it is a problem.\n\n' +
    '#### Scenario: An unreachable source is refused rather than rendered empty\n- GIVEN ...\n',
};

/**
 * Each case names the rule, the mutation, and the substring the error must contain.
 * `null` content deletes the file.
 */
const CASES = [
  {
    rule: 'okf/index.md must declare okf_version',
    files: { 'okf/index.md': '---\ntitle: Bundle\n---\n\n# Bundle\n' },
    expect: 'okf_version',
  },
  {
    rule: 'okf_version declared outside index.md',
    files: { 'okf/service.md': CLEAN['okf/service.md'].replace('type: Service', 'okf_version: "0.1"\ntype: Service') },
    expect: 'declared in okf/index.md only',
  },
  { rule: 'okf/service.md missing', files: { 'okf/service.md': null }, expect: 'okf/service.md missing' },
  {
    rule: 'okf/service.md missing required frontmatter',
    files: { 'okf/service.md': '---\ntype: Service\ntitle: Fixture\n---\n' },
    expect: 'missing required frontmatter: description',
  },
  {
    rule: 'concept with no frontmatter',
    files: { 'okf/lib/thing.md': '# No frontmatter here\n' },
    expect: 'missing YAML frontmatter',
  },
  {
    rule: 'concept with empty type',
    files: { 'okf/lib/thing.md': '---\ntype:\ntitle: T\n---\n' },
    expect: "missing non-empty 'type'",
  },
  {
    rule: 'frontmatter status: other than stub',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\nstatus: accepted\n---\n' },
    expect: 'status lives in the roadmap',
  },
  {
    rule: 'frontmatter status: stub is allowed',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\nstatus: stub\n---\n' },
    expectPass: true,
  },
  {
    rule: 'retired type: PRD',
    files: { 'okf/lib/thing.md': '---\ntype: PRD\ntitle: T\n---\n' },
    expect: "retired type 'PRD'",
  },
  {
    rule: 'retired type: TRD',
    files: { 'okf/lib/thing.md': '---\ntype: TRD\ntitle: T\n---\n' },
    expect: "retired type 'TRD'",
  },
  {
    rule: 'retired directory okf/prds/',
    files: { 'okf/prds/x.md': '---\ntype: Business Concept\n---\n' },
    expect: 'okf/prds/ exists',
  },
  {
    rule: 'retired directory okf/trd/',
    files: { 'okf/trd/x.md': '---\ntype: Business Concept\n---\n' },
    expect: 'okf/trd/ exists',
  },
  {
    rule: 'PRDs parked in _proposals/ are exempt',
    files: {
      'okf/_proposals/prds/old.md': '---\ntype: PRD\nstatus: accepted\n---\n\n# Old PRD\n\nStatus glyph ok here too.\n',
    },
    expectPass: true,
  },
  {
    rule: 'status glyph outside roadmap.md and log.md',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\n---\n\n| Done |\n|---|\n| ☑ |\n' },
    expect: 'only status surface',
  },
  {
    rule: 'status glyph in log.md is exempt',
    files: { 'okf/log.md': '# Log\n\n- 2026-09-02 ✅ landed\n' },
    expectPass: true,
  },
  {
    rule: 'brief with no ## Narrative',
    files: { 'okf/briefs/a-thing.md': CLEAN['okf/briefs/a-thing.md'].replace('## Narrative', '## Background') },
    expect: "no '## Narrative'",
  },
  {
    rule: 'brief with no scenario',
    files: {
      'okf/briefs/a-thing.md': '---\ntype: Brief\ntitle: A\ndescription: d\n---\n\n## Narrative\nWords.\n',
    },
    expect: "brief has no '#### Scenario:'",
  },
  {
    rule: 'capability with no scenario',
    files: { 'okf/capabilities/demo.md': '---\ntype: Capability\ntitle: Demo\ndescription: d\n---\n\nWorks correctly.\n' },
    expect: "capability has no '#### Scenario:'",
  },
  {
    rule: 'scenario with no Checked by',
    files: {
      'okf/capabilities/demo.md': CLEAN['okf/capabilities/demo.md'].replace(
        /\*\*Checked by:\*\* demo-refuses-when-unreachable\n/,
        ''
      ),
    },
    expect: "has no '**Checked by:**'",
  },
  {
    rule: 'capability with no QA checklist',
    files: { 'okf/qa/demo.md': null },
    expect: 'no okf/qa/demo.md',
  },
  {
    rule: 'forwards traceability — cited check does not exist',
    files: {
      'okf/capabilities/demo.md': CLEAN['okf/capabilities/demo.md'].replace(
        'demo-refuses-when-unreachable',
        'demo-typo-in-the-citation'
      ),
    },
    expect: 'absent from okf/qa/demo.md',
  },
  {
    rule: 'backwards traceability — check outlived its requirement',
    files: {
      'okf/capabilities/demo.md': CLEAN['okf/capabilities/demo.md'].replace(
        '#### Scenario: An unreachable source is refused rather than rendered empty',
        '#### Scenario: An unreachable source is refused, reworded without updating the check'
      ),
    },
    expect: 'which no scenario in',
  },
  {
    rule: 'checklist with no checks',
    files: { 'okf/qa/demo.md': '---\ntype: QA Checklist\ntitle: D\ndescription: d\n---\n\nNothing here.\n' },
    expect: "no '### Check:' entries",
  },
  {
    rule: 'duplicate check id',
    files: {
      'okf/qa/demo.md':
        CLEAN['okf/qa/demo.md'] +
        '\n### Check: demo-refuses-when-unreachable\n**Requirement:** An unreachable source is refused rather than rendered empty\n**Surface:** s\n**Automated:** Manual\n\n**Do**\n- x\n\n**Expect**\n- y\n',
    },
    expect: 'duplicate check id',
  },
  {
    rule: 'check missing Automated',
    files: { 'okf/qa/demo.md': CLEAN['okf/qa/demo.md'].replace('**Automated:** Manual\n', '') },
    expect: "missing '**Automated:**'",
  },
  {
    rule: 'check missing Surface',
    files: { 'okf/qa/demo.md': CLEAN['okf/qa/demo.md'].replace('**Surface:** Public route\n', '') },
    expect: "missing '**Surface:**'",
  },
  {
    rule: 'check with no Expect block',
    files: {
      'okf/qa/demo.md': CLEAN['okf/qa/demo.md'].replace(
        '**Expect**\n- A 503 with a Retry-After, and no empty body served\n',
        ''
      ),
    },
    expect: "no non-empty '**Expect**' block",
  },
  {
    rule: 'check with an EMPTY Expect block still cannot fail',
    files: {
      'okf/qa/demo.md': CLEAN['okf/qa/demo.md'].replace(
        '**Expect**\n- A 503 with a Retry-After, and no empty body served\n',
        '**Expect**\n'
      ),
    },
    expect: "no non-empty '**Expect**' block",
  },
  {
    rule: 'check with no Do block',
    files: {
      'okf/qa/demo.md': CLEAN['okf/qa/demo.md'].replace('**Do**\n- Stop the source and request the page\n', ''),
    },
    expect: "no non-empty '**Do**' block",
  },
  {
    rule: 'checklist with no capability',
    files: {
      'okf/qa/orphan.md':
        '---\ntype: QA Checklist\ntitle: Orphan\ndescription: d\n---\n\n### Check: orphan-check\n**Requirement:** Something nobody declares\n**Surface:** s\n**Automated:** Manual\n\n**Do**\n- x\n\n**Expect**\n- y\n',
    },
    expect: 'no okf/capabilities/orphan.md',
  },
  {
    rule: 'broken same-repo link',
    files: { 'okf/index.md': CLEAN['okf/index.md'].replace('/service.md', '/nope.md') },
    expect: 'broken same-repo link',
  },
  {
    rule: 'broken repo-file link',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\n---\n\n[rules](../../.claude/rules/nope.md)\n' },
    expect: 'broken repo-file link',
  },
  {
    rule: 'broken BARE relative link (no leading ./)',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\n---\n\n[prds](prds/index.md)\n' },
    expect: 'broken repo-file link',
  },
  {
    rule: 'valid bare relative link is accepted',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\n---\n\n[svc](../service.md)\n[log](../log.md)\n' },
    expectPass: true,
  },
  {
    rule: 'cross-repo link is soft, not an error',
    files: { 'okf/lib/thing.md': '---\ntype: Service Module\n---\n\n[x](aeo-backend:/models/thing.md)\n' },
    expectPass: true,
  },
];

function build(dir, overrides) {
  fs.rmSync(dir, { recursive: true, force: true });
  const merged = { ...CLEAN, ...overrides };
  for (const [rel, content] of Object.entries(merged)) {
    if (content === null) continue;
    const full = path.join(dir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
}

function run(dir) {
  try {
    const out = execFileSync(process.execPath, [CHECKER], { cwd: dir, encoding: 'utf8', stdio: 'pipe' });
    return { ok: true, out };
  } catch (e) {
    return { ok: false, out: (e.stdout || '') + (e.stderr || '') };
  }
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'okf-check-test-'));
let failures = 0;
let n = 0;

// The clean bundle must pass, or every negative case below proves nothing.
build(tmp, {});
const base = run(tmp);
n++;
if (!base.ok) {
  failures++;
  console.error('FAIL  the clean fixture bundle does not pass:\n' + base.out);
} else {
  console.log('ok    clean fixture bundle passes');
}

for (const c of CASES) {
  n++;
  build(tmp, c.files);
  const r = run(tmp);
  if (c.expectPass) {
    if (r.ok) console.log(`ok    ${c.rule} — accepted`);
    else {
      failures++;
      console.error(`FAIL  ${c.rule} — should pass but failed:\n${r.out.trim()}`);
    }
    continue;
  }
  if (r.ok) {
    failures++;
    console.error(`FAIL  ${c.rule} — checker did NOT fail (rule is not firing)`);
  } else if (!r.out.includes(c.expect)) {
    failures++;
    console.error(`FAIL  ${c.rule} — failed for the wrong reason; wanted "${c.expect}":\n${r.out.trim()}`);
  } else {
    console.log(`ok    ${c.rule} — fires`);
  }
}

fs.rmSync(tmp, { recursive: true, force: true });

if (failures) {
  console.error(`\n${failures} of ${n} checker proofs FAILED`);
  process.exit(1);
}
console.log(`\nAll ${n} checker proofs passed — every rule was shown to fire.`);
