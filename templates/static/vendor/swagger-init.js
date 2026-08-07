/* Swagger UI initialisation, as a FILE.
 *
 * FastAPI's get_swagger_ui_html() emits this as an inline <script>, which
 * `script-src-elem 'self'` blocks — so self-hosting the bundle got the assets
 * loaded and the page still rendered nothing, because the call that mounts it
 * never ran. Moving the call into a file is the fix that needs no CSP change,
 * no nonce plumbing and no hash to keep in sync. */
window.addEventListener('DOMContentLoaded', function () {
    window.ui = SwaggerUIBundle({
        url: '/openapi.json',
        dom_id: '#swagger-ui',
        layout: 'BaseLayout',
        deepLinking: true,
        showExtensions: true,
        showCommonExtensions: true,
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset]
    });
});
