// Flat config. Deliberately small: the type checker does most of the work here,
// so eslint only carries the rules tsc cannot express.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      '**/dist/**',
      '**/build/**',
      '**/node_modules/**',
      '**/*.d.ts',
      '.tools/**',
      // The Python virtualenv ships vendored JavaScript (coverage's HTML report,
      // urllib3's emscripten worker) that is not ours to lint.
      '.venv/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        // An explicit lint-only project rather than projectService: tests and
        // this config file are not in the build tsconfig, and projectService
        // fails on any file no tsconfig.json claims.
        project: ['./tsconfig.eslint.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // A Lambda handler that swallows a rejected promise silently drops logs.
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      // Use the structured logger, not console, so lines land in CloudWatch as JSON.
      'no-console': 'error',
      'no-restricted-syntax': [
        'error',
        {
          selector: "NewExpression[callee.name='Date'][arguments.length=0]",
          message: 'Pass an explicit epoch; a bare new Date() hides timestamp bugs.',
        },
      ],
    },
  },
  {
    files: ['**/test/**/*.ts', '**/*.test.ts'],
    rules: { '@typescript-eslint/no-non-null-assertion': 'off' },
  },
  {
    // This config file itself is plain JS, so the type-aware rules have no
    // program to work from.
    files: ['**/*.mjs', '**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
  },
);
