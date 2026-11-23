#!/usr/bin/env bun

import { spawnSync } from "node:child_process";

const FRONTEND_TEST_FILE_PATTERN = /^tests\/frontend\/[^/]+\.test\.(?:mjs|ts)$/;
const FULL_RUN_PATTERNS = [
  /^astro\.config\./,
  /^bun\.lock$/,
  /^package\.json$/,
  /^tsconfig\.json$/,
  /^vitest\.config\./,
  /^src\/env\.d\.ts$/,
  /^src\/data\//,
  /^src\/lib\/data\.ts$/,
  /^src\/pages\//,
  /^tests\/frontend\/(?![^/]+\.test\.(?:mjs|ts)$)/,
];

const TEST_RULES = [
  [
    /^public\/scripts\/guide-filters\.js$/,
    ["tests/frontend/guideFilters.test.mjs", "tests/frontend/guideMapInteractions.test.mjs"],
  ],
  [/^public\/scripts\/home-browser-nearby\.js$/, ["tests/frontend/homeBrowserNearby.test.mjs"]],
  [/^public\/scripts\/home-browser-state\.js$/, ["tests/frontend/homeBrowserState.test.mjs"]],
  [/^public\/scripts\/place-search\.js$/, ["tests/frontend/placeSearch.test.mjs"]],
  [/^public\/scripts\/scroll-jump-core\.js$/, ["tests/frontend/scrollJump.test.mjs"]],
  [/^scripts\/worktree_dev_port\.mjs$/, ["tests/frontend/worktreeDevPort.test.mjs"]],
  [/^src\/lib\/authorAvatar\.ts$/, ["tests/frontend/authorAvatar.test.ts"]],
  [/^src\/lib\/mapMarkerIcons\.ts$/, ["tests/frontend/mapMarkerIcons.test.ts"]],
  [/^src\/lib\/placeTags\.ts$/, ["tests/frontend/placeTags.test.ts"]],
  [/^src\/lib\/renderRichText\.ts$/, ["tests/frontend/renderRichText.test.ts"]],
  [/^src\/lib\/seo\.ts$/, ["tests/frontend/seo.test.ts"]],
  [/^src\/lib\/sitePaths\.ts$/, ["tests/frontend/seo.test.ts"]],
  [/^src\/lib\/types\.ts$/, ["tests/frontend/seo.test.ts"]],
  [/^src\/middleware\.ts$/, ["tests/frontend/middleware.test.ts"]],
  [/^src\/styles\/.+\.css$/, ["tests/frontend/guideMapInteractions.test.mjs"]],
  [/^src\/components\/.+\.astro$/, ["tests/frontend/guideMapInteractions.test.mjs"]],
];

function normalizePath(path) {
  return path.trim().replace(/^\.\/+/, "");
}

function parseChangedFiles(value) {
  if (!value) {
    return [];
  }

  try {
    const parsed = JSON.parse(value);
    if (Array.isArray(parsed)) {
      return parsed.map(String).map(normalizePath).filter(Boolean);
    }
  } catch {
    // Fall through to whitespace parsing for local use and shell-formatted CI lists.
  }

  return value
    .split(/\s+/)
    .map((part) => part.replace(/^['"]|['"]$/g, ""))
    .map(normalizePath)
    .filter(Boolean);
}

function changedFilesFromGit(baseRef) {
  if (!baseRef) {
    return [];
  }

  const diff = spawnSync(
    "git",
    ["diff", "--name-only", "--diff-filter=ACMRT", `${baseRef}...HEAD`],
    {
      encoding: "utf8",
    },
  );
  if (diff.status !== 0) {
    return [];
  }

  return parseChangedFiles(diff.stdout);
}

function resolveChangedFiles() {
  const filesFromEnv = parseChangedFiles(process.env.FRONTEND_CHANGED_FILES);
  if (filesFromEnv.length > 0) {
    return filesFromEnv;
  }

  const baseRef = process.env.FRONTEND_CHANGED_BASE_REF ?? "origin/main";
  return changedFilesFromGit(baseRef);
}

function selectTests(changedFiles) {
  const selected = new Set();

  for (const file of changedFiles) {
    if (FRONTEND_TEST_FILE_PATTERN.test(file)) {
      selected.add(file);
      continue;
    }

    if (FULL_RUN_PATTERNS.some((pattern) => pattern.test(file))) {
      return { fullRun: true, reason: file, tests: [] };
    }

    const matchingRule = TEST_RULES.find(([pattern]) => pattern.test(file));
    if (!matchingRule) {
      return { fullRun: true, reason: file, tests: [] };
    }

    for (const testFile of matchingRule[1]) {
      selected.add(testFile);
    }
  }

  return { fullRun: false, reason: "", tests: [...selected].sort() };
}

function runVitest(args) {
  const command = ["x", "vitest", "run", ...args];
  console.log(`$ bun ${command.join(" ")}`);

  const result = spawnSync("bun", command, {
    env: process.env,
    stdio: "inherit",
  });

  process.exit(result.status ?? 1);
}

if (process.env.FULL_FRONTEND_TESTS === "true") {
  console.log("Running the full frontend test suite because workflow files changed.");
  runVitest([]);
}

const changedFiles = resolveChangedFiles();
if (changedFiles.length === 0) {
  console.log("No changed frontend files detected; skipping frontend tests.");
  process.exit(0);
}

const selection = selectTests(changedFiles);
if (selection.fullRun) {
  console.log(`Running the full frontend test suite because ${selection.reason} has broad impact.`);
  runVitest([]);
}

if (selection.tests.length === 0) {
  console.log("No frontend tests matched the changed files; skipping frontend tests.");
  process.exit(0);
}

console.log(`Running ${selection.tests.length} selected frontend test file(s).`);
runVitest(selection.tests);
