const js = require('@eslint/js');
const tsParser = require('@typescript-eslint/parser');
const tsPlugin = require('@typescript-eslint/eslint-plugin');

module.exports = [
    js.configs.recommended,
    {
        files: ['**/*.ts'],
        languageOptions: {
            parser: tsParser,
            parserOptions: {
                ecmaVersion: 2020,
                sourceType: 'module',
            },
            globals: {
                console: 'readonly',
                process: 'readonly',
                __dirname: 'readonly',
                __filename: 'readonly',
                setTimeout: 'readonly',
                clearTimeout: 'readonly',
                setInterval: 'readonly',
                clearInterval: 'readonly',
                Buffer: 'readonly',
                global: 'readonly',
                module: 'readonly',
                require: 'readonly',
                exports: 'readonly',
                NodeJS: 'readonly',
                AbortController: 'readonly',
                fetch: 'readonly',
                ReadableStream: 'readonly',
                TextDecoder: 'readonly',
            },
        },
        plugins: {
            '@typescript-eslint': tsPlugin,
        },
        rules: {
            ...tsPlugin.configs.recommended.rules,
            // 警告级别检查
            '@typescript-eslint/no-unused-vars': 'warn',
            'no-console': ['warn', { allow: ['warn', 'error', 'info', 'debug'] }],
            'no-undef': 'warn',

            // 关闭严格检查
            '@typescript-eslint/explicit-function-return-type': 'off',
            '@typescript-eslint/explicit-module-boundary-types': 'off',
            '@typescript-eslint/no-explicit-any': 'off',
            '@typescript-eslint/ban-ts-comment': 'off',
            '@typescript-eslint/no-non-null-asserted-optional-chain': 'off',
            'no-useless-escape': 'off',
            'no-case-declarations': 'off',
        },
    },
    {
        ignores: ['dist/', 'node_modules/', '*.js'],
    },
];