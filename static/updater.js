/* Update controls stay available while the independent service restarts the app. */
(() => {
    const button = document.getElementById("updateGithubBtn");
    const status = document.getElementById("updateGithubStatus");
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    let watching = false;

    function show(message, failed = false) {
        status.textContent = message;
        status.classList.toggle("update-error", failed);
    }

    async function readState() {
        const response = await fetch("/update/status", {
            cache: "no-store", signal: AbortSignal.timeout(5000)
        });
        if (!response.ok) throw new Error("Cannot read update status.");
        return response.json();
    }

    async function watch(requestId) {
        watching = true;
        button.disabled = true;
        button.textContent = "Updating…";
        const deadline = Date.now() + 11 * 60 * 1000;
        try {
            while (Date.now() < deadline) {
                await wait(1500);
                try {
                    const state = await readState();
                    if (requestId && state.request_id !== requestId) {
                        show("Waiting for the chamber to reconnect…");
                        continue;
                    }
                    show(state.message, ["failed", "rolled_back"].includes(state.status));
                    if (!state.busy) {
                        if (state.status === "succeeded") {
                            // Reload only after the worker confirms the new version is healthy.
                            window.location.reload();
                        }
                        return;
                    }
                } catch (_error) {
                    show("Reconnecting to the chamber website…");
                }
            }
            show("The update is taking longer than expected. Reload to check its status.", true);
        } finally {
            watching = false;
            button.disabled = false;
            button.textContent = "Update from GitHub";
        }
    }

    button.addEventListener("click", async () => {
        if (watching) return;
        button.disabled = true;
        try {
            const response = await fetch("/status", {
                cache: "no-store", signal: AbortSignal.timeout(5000)
            });
            if (!response.ok) throw new Error("Cannot verify collection status. Try again when the chamber is online.");
            const chamber = await response.json();
            if (chamber.running || chamber.collector_active) {
                show("Stop collection and wait for the sensors to finish before updating.", true);
                return;
            }
            if (!window.confirm("Install the latest version from GitHub? The website may briefly restart. Your recordings and device settings will be kept.")) {
                return;
            }
            show("Starting update check…");
            const update = await fetch("/update", {
                method: "POST",
                headers: {"Content-Type": "application/json", "X-Chamber-Update": button.dataset.updateToken},
                body: "{}",
                signal: AbortSignal.timeout(15000)
            });
            const state = await update.json();
            if (!update.ok) throw new Error(state.message || "The update could not start.");
            await watch(state.request_id);
        } catch (error) {
            show(error.message, true);
            // A lost response can still have started a worker. Recover its status.
            try {
                const state = await readState();
                if (state.busy) await watch(state.request_id);
            } catch (_error) { /* Keep the original error visible. */ }
        } finally {
            button.disabled = false;
        }
    });

    // Resume monitoring when another user started an update or the page reloaded.
    readState().then(state => {
        if (state.busy) {
            watch(state.request_id);
        } else if (state.status !== "idle") {
            show(state.message, ["failed", "rolled_back"].includes(state.status));
        }
    }).catch(() => show("Update status is unavailable. Refresh the page to retry.", true));
})();
