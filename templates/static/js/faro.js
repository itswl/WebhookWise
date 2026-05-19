(function () {
    const localCollectorHosts = new Set(["localhost", "127.0.0.1"]);
    const defaultCollectorUrl = localCollectorHosts.has(window.location.hostname)
        ? `${window.location.protocol}//${window.location.hostname}:12347/collect`
        : "";

    const collectorUrl = window.WEBHOOKWISE_FARO_URL
        || window.localStorage.getItem("webhookwise:faroUrl")
        || defaultCollectorUrl;

    if (!collectorUrl || window.__WEBHOOKWISE_FARO_INITIALIZED__) {
        return;
    }

    window.__WEBHOOKWISE_FARO_INITIALIZED__ = true;

    const script = document.createElement("script");
    script.src = "https://unpkg.com/@grafana/faro-web-sdk@^1.0.0-beta/dist/bundle/faro-web-sdk.iife.js";
    script.crossOrigin = "anonymous";
    script.onload = function () {
        const sdk = window.GrafanaFaroWebSdk;
        if (!sdk || typeof sdk.initializeFaro !== "function") {
            return;
        }

        const config = {
            url: collectorUrl,
            app: {
                name: "webhookwise-dashboard",
                version: window.WEBHOOKWISE_VERSION || "local",
                environment: window.location.hostname === "localhost" ? "local" : "development",
            },
        };
        const apiKey = window.WEBHOOKWISE_FARO_API_KEY || window.localStorage.getItem("webhookwise:faroApiKey");
        if (apiKey) {
            config.apiKey = apiKey;
        }

        sdk.initializeFaro(config);
    };
    document.head.appendChild(script);
})();
