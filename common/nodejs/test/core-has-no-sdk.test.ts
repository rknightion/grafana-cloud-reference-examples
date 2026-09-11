import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * The package root must not pull the AWS SDK in.
 *
 * This is the test that stops the dependency split in the README from rotting. A
 * single stray `@aws-sdk` import in loki.ts or config.ts adds megabytes to every
 * example's deployment package, and nothing else in the gate would notice.
 *
 * A static check on the source rather than a runtime probe: strictly stronger
 * here, because it catches a type-only import too, which erases at runtime and
 * would pass a module-registry check while still being wrong.
 */

const CORE_FILES = ['index.ts', 'config.ts', 'errors.ts', 'logger.ts', 'loki.ts', 'batching.ts'];

function sourcesIn(directory: string): string[] {
  return readdirSync(join('src', directory), { withFileTypes: true })
    .filter((item) => item.isFile() && item.name.endsWith('.ts'))
    .map((item) => join('src', directory, item.name));
}

describe('the core entry point', () => {
  it.each(CORE_FILES)('src/%s does not import the AWS SDK', (name) => {
    expect(readFileSync(join('src', name), 'utf8')).not.toMatch(/@aws-sdk/);
  });

  it('covers every file in src/ that is not under aws/', () => {
    // Guards the list above: a new core module added without being listed here
    // would otherwise never be checked.
    const found = readdirSync('src', { withFileTypes: true })
      .filter((item) => item.isFile() && item.name.endsWith('.ts'))
      .map((item) => item.name)
      .sort();
    expect(found).toEqual([...CORE_FILES].sort());
  });

  it('the aws subtree does import the SDK, so the checks above are meaningful', () => {
    // The inverse assertion: without it, the checks above could pass simply by
    // the SDK not being used anywhere at all.
    const awsSources = sourcesIn('aws').map((path) => readFileSync(path, 'utf8'));
    expect(awsSources.some((source) => /@aws-sdk/.test(source))).toBe(true);
  });
});
