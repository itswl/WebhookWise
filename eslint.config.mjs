// Frontend lint: high-signal correctness rules only. The dashboard is
// deliberately buildless vanilla JS wired through shared globals, so
// no-undef would demand a hundred-entry globals table for zero insight —
// the contract tests own cross-file integrity instead.
export default [
    {
        files: ["templates/static/js/**/*.js"],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "script",
        },
        rules: {
            "no-dupe-keys": "error",
            "no-dupe-args": "error",
            "no-duplicate-case": "error",
            "no-unreachable": "error",
            "no-compare-neg-zero": "error",
            "no-cond-assign": "error",
            "no-constant-condition": ["error", { checkLoops: false }],
            "no-self-assign": "error",
            "no-self-compare": "error",
            "use-isnan": "error",
            "valid-typeof": "error",
            "no-template-curly-in-string": "error",
            "no-unsafe-negation": "error",
        },
    },
];
