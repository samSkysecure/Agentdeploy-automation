document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("onboard-form");
    const terminalOutput = document.getElementById("terminal-output");
    const deployBtn = document.getElementById("deploy-btn");
    const btnText = deployBtn.querySelector(".btn-text");
    const loader = deployBtn.querySelector(".loader");

    const successModal = document.getElementById("success-modal");
    const downloadManifestBtn = document.getElementById("download-manifest-btn");
    const closeSuccessBtn = document.getElementById("close-success-btn");

    // Hardcoded per the "prove it on docgen first, no generalization yet" scope.
    const AGENT_SLUG = "docgen";
    const CUSTOMER_SLUG = "skysecure";

    let pollTimer = null;
    let currentDeploymentId = null;

    function appendLog(message, type = "normal") {
        const line = document.createElement("div");
        line.className = `log-line ${type}`;
        line.textContent = message;
        terminalOutput.appendChild(line);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }

    closeSuccessBtn.addEventListener("click", () => {
        successModal.classList.add("hidden");
    });

    // ---------------------------------------------------------------------
    // Step 1: deployment SPN client ID
    // ---------------------------------------------------------------------
    let spClientId = "";
    const spClientIdSpan = document.getElementById("sp-client-id");
    const copySpBtn = document.getElementById("copy-sp-btn");
    const tenantIdInput = document.getElementById("tenantId");
    const btnAdminConsent = document.getElementById("btn-admin-consent");

    async function loadSpDetails() {
        try {
            const res = await fetch("/api/azure/sp-details");
            if (res.ok) {
                const data = await res.json();
                spClientId = data.clientId;
                spClientIdSpan.textContent = spClientId;
                updateConsentLink();
            } else {
                spClientIdSpan.textContent = "Error loading Client ID";
            }
        } catch (err) {
            spClientIdSpan.textContent = "Error loading Client ID";
        }
    }
    loadSpDetails();

    let resolvedTenantId = null;
    let resolveTimer = null;

    const tenantResolvedBadge = document.getElementById("tenant-resolved-badge");
    const resolvedTenantIdText = document.getElementById("resolved-tenant-id-text");
    const subscriptionSelect = document.getElementById("subscriptionSelect");
    const assignRoleSubscriptionIdInput = document.getElementById("assignRoleSubscriptionId");
    const subRefreshStatus = document.getElementById("sub-refresh-status");

    // ---------------------------------------------------------------------
    // Step 2: Tenant resolution (no longer auto-fetches subscriptions)
    // Subscriptions are now loaded manually via "Refresh" in Step 4 — after
    // Lighthouse delegation has been completed in Step 3.
    // ---------------------------------------------------------------------
    async function resolveTenant() {
        const query = tenantIdInput.value.trim();
        if (!query) {
            resolvedTenantId = null;
            if (tenantResolvedBadge) tenantResolvedBadge.classList.add("hidden");
            updateConsentLink();
            return;
        }

        try {
            const res = await fetch(`/api/azure/resolve-tenant-id?query=${encodeURIComponent(query)}`);
            if (res.ok) {
                const data = await res.json();
                resolvedTenantId = data.tenantId;
                if (resolvedTenantIdText) resolvedTenantIdText.textContent = resolvedTenantId;
                if (tenantResolvedBadge) tenantResolvedBadge.classList.remove("hidden");
                updateConsentLink();
                // NOTE: subscriptions are NOT auto-fetched here anymore.
                // The user must complete Lighthouse (Step 3) first, then click
                // "Refresh Subscriptions" (Step 4) to populate the dropdown.
            } else {
                resolvedTenantId = null;
                if (tenantResolvedBadge) tenantResolvedBadge.classList.add("hidden");
                updateConsentLink();
            }
        } catch (err) {
            resolvedTenantId = null;
            if (tenantResolvedBadge) tenantResolvedBadge.classList.add("hidden");
            updateConsentLink();
        }
    }

    tenantIdInput.addEventListener("input", () => {
        clearTimeout(resolveTimer);
        resolveTimer = setTimeout(resolveTenant, 350);
    });

    // ---------------------------------------------------------------------
    // Step 4: Subscription fetch — MANUAL via "Refresh" button.
    // Called only after the user has completed Lighthouse delegation (Step 3).
    // The /subscriptions endpoint now authenticates from Skysecure's managing
    // tenant, so subscriptions appear only after Lighthouse delegation grants
    // cross-tenant ARM access from Skysecure's side.
    // ---------------------------------------------------------------------
    async function fetchSubscriptions() {
        const tenantId = resolvedTenantId || tenantIdInput.value.trim();
        if (!tenantId) {
            setSubRefreshStatus("Enter your work email/domain in Step 2 first.", "error");
            return;
        }

        if (!subscriptionSelect) return;

        subscriptionSelect.innerHTML = `<option value="">-- Discovering Subscriptions... --</option>`;
        setSubRefreshStatus("Contacting Azure Lighthouse delegation...", "info");

        const btnRefresh = document.getElementById("btn-refresh-subs");
        if (btnRefresh) { btnRefresh.disabled = true; btnRefresh.textContent = "Refreshing..."; }

        try {
            const res = await fetch(`/api/azure/subscriptions?tenant_id=${tenantId}`);
            if (res.ok) {
                const subs = await res.json();
                if (subs && subs.length > 0) {
                    subscriptionSelect.innerHTML = `<option value="">-- Select a subscription --</option>` + subs.map(s =>
                        `<option value="${s.subscriptionId}">${s.displayName || s.subscriptionId} (${s.subscriptionId.substring(0, 8)}...)</option>`
                    ).join("");
                    assignRoleSubscriptionIdInput.value = "";
                    setSubRefreshStatus(`✓ Found ${subs.length} subscription(s). Select one below.`, "success");
                } else {
                    subscriptionSelect.innerHTML = `<option value="">-- No subscriptions found --</option>`;
                    setSubRefreshStatus("No subscriptions found. Make sure Lighthouse delegation (Step 3) completed successfully.", "warn");
                }
            } else {
                const errorData = await res.json().catch(() => ({}));
                subscriptionSelect.innerHTML = `<option value="">-- Lighthouse delegation not yet complete --</option>`;
                setSubRefreshStatus(
                    errorData.detail || "Lighthouse delegation not yet detected. Complete Step 3, wait ~30s, then refresh.",
                    "error"
                );
            }
        } catch (err) {
            subscriptionSelect.innerHTML = `<option value="">-- Error fetching subscriptions --</option>`;
            setSubRefreshStatus(`Error: ${err.message}`, "error");
        } finally {
            if (btnRefresh) { btnRefresh.disabled = false; btnRefresh.textContent = "Refresh"; }
        }
    }

    function setSubRefreshStatus(msg, type) {
        if (!subRefreshStatus) return;
        subRefreshStatus.textContent = msg;
        subRefreshStatus.className = `form-alert ${type}`;
        subRefreshStatus.classList.remove("hidden");
    }

    // Wire up the Refresh Subscriptions button (Step 4)
    const btnRefreshSubs = document.getElementById("btn-refresh-subs");
    if (btnRefreshSubs) {
        btnRefreshSubs.addEventListener("click", fetchSubscriptions);
    }

    if (subscriptionSelect) {
        subscriptionSelect.addEventListener("change", () => {
            if (assignRoleSubscriptionIdInput) {
                assignRoleSubscriptionIdInput.value = subscriptionSelect.value;
            }
        });
    }

    function updateConsentLink() {
        const tid = resolvedTenantId || tenantIdInput.value.trim();
        if (tid && spClientId) {
            btnAdminConsent.href = `https://login.microsoftonline.com/${tid}/adminconsent?client_id=${spClientId}`;
            btnAdminConsent.classList.remove("disabled");
        } else {
            btnAdminConsent.removeAttribute("href");
            btnAdminConsent.classList.add("disabled");
        }
    }

    copySpBtn.addEventListener("click", () => {
        const id = spClientIdSpan.textContent;
        if (id && id !== "Loading..." && !id.startsWith("Error")) {
            navigator.clipboard.writeText(id);
            copySpBtn.textContent = "Copied!";
            setTimeout(() => copySpBtn.textContent = "Copy", 2000);
        }
    });

    // ---------------------------------------------------------------------
    // Step 3: Lighthouse Delegation button — no subscription ID required.
    // The Lighthouse ARM template is deployed via the Azure portal "Deploy to
    // Azure" link. The customer selects their subscription IN the portal when
    // the template deploys — we do not need to know it beforehand.
    // After the portal deployment is done, the user clicks "Refresh" in Step 4.
    // ---------------------------------------------------------------------
    const btnAssignRole = document.getElementById("btn-assign-role");
    const assignRoleAlert = document.getElementById("assign-role-alert");
    const assignRoleInstructions = document.getElementById("assign-role-instructions");
    const assignRoleCliBox = document.getElementById("assign-role-cli-box");
    const assignRoleCliSpan = document.getElementById("assign-role-cli");
    const copyAssignRoleCliBtn = document.getElementById("copy-assign-role-cli-btn");

    function showAssignRoleAlert(msg, type) {
        assignRoleAlert.textContent = msg;
        assignRoleAlert.className = `form-alert ${type}`;
        assignRoleAlert.classList.remove("hidden");
    }

    btnAssignRole.addEventListener("click", async () => {
        // Use a placeholder subscription ID since the ARM template is scoped
        // at subscription level but the CUSTOMER picks the subscription when
        // they open the portal — we just need any valid URL format.
        // The backend assign-role-link endpoint builds the portal URL; we pass
        // a sentinel value to indicate "no sub yet" and let the backend return
        // the subscription-agnostic portal deployment URL.
        const subId = assignRoleSubscriptionIdInput.value.trim() || subscriptionSelect?.value || "pending";

        btnAssignRole.disabled = true;
        showAssignRoleAlert("Opening Azure Portal for Lighthouse delegation...", "info");
        try {
            // Use subId if already selected, otherwise use 'pending' sentinel.
            // The portal deployment URL works regardless — customer picks subscription there.
            const querySubId = (subId && subId !== "pending") ? subId : "00000000-0000-0000-0000-000000000000";
            const res = await fetch(`/api/azure/assign-role-link?subscription_id=${querySubId}`);
            if (!res.ok) {
                const errorData = await res.json().catch(() => ({}));
                throw new Error(errorData.detail || "Failed to build the role assignment link.");
            }
            const data = await res.json();
            window.open(data.portalUrl, "_blank");
            assignRoleInstructions.textContent = data.instructions;
            assignRoleCliSpan.textContent = data.powershellFallback;
            assignRoleCliBox.classList.remove("hidden");
            showAssignRoleAlert(
                "Azure Portal opened in a new tab. Select your subscription in the portal, review the template parameters, then click Create. Once complete, return here and click 'Refresh' in Step 4.",
                "info"
            );
        } catch (err) {
            showAssignRoleAlert(err.message, "error");
        } finally {
            btnAssignRole.disabled = false;
        }
    });

    if (copyAssignRoleCliBtn) {
        copyAssignRoleCliBtn.addEventListener("click", () => {
            const cmd = assignRoleCliSpan.textContent;
            if (cmd) {
                navigator.clipboard.writeText(cmd);
                copyAssignRoleCliBtn.textContent = "Copied!";
                setTimeout(() => copyAssignRoleCliBtn.textContent = "Copy", 2000);
            }
        });
    }

    // ---------------------------------------------------------------------
    // Step 5: Deployment
    // ---------------------------------------------------------------------

    // Step 5: Power Platform environment is now auto-discovered mid-deployment.
    // No manual environment ID field — the wizard will show a dropdown in the
    // auth modal if multiple environments are found and none can be auto-selected.
    // ---------------------------------------------------------------------

    form.addEventListener("submit", async (e) => {
        e.preventDefault();

        btnText.classList.add("hidden");
        loader.classList.remove("hidden");
        deployBtn.disabled = true;
        terminalOutput.innerHTML = "";
        appendLog("Initializing deployment sequence...", "system");

        const tenantId = resolvedTenantId || tenantIdInput.value.trim();
        if (!tenantId) {
            appendLog("Work Email, Domain or Tenant ID is required (Step 2).", "error");
            resetUI();
            return;
        }
        const subscriptionId = assignRoleSubscriptionIdInput.value.trim() || (subscriptionSelect ? subscriptionSelect.value : "");
        if (!subscriptionId) {
            appendLog("Subscription ID is required (Step 4). Complete Lighthouse delegation (Step 3) and refresh subscriptions.", "error");
            resetUI();
            return;
        }

        // Resolve the deployment SPN's Service Principal object ID in the
        // customer tenant - required by the pipeline for the role-assignment
        // verification step and for teardown at the end. Only resolvable
        // after admin consent has actually happened.
        appendLog("Resolving deployment SPN identity in the customer tenant...", "system");
        let deploymentSpnObjectId;
        try {
            const spRes = await fetch(`/api/azure/deployment-spn-object-id?tenant_id=${tenantId}`);
            if (!spRes.ok) {
                const errorData = await spRes.json().catch(() => ({}));
                throw new Error(errorData.detail || "Could not resolve the deployment SPN in this tenant - has admin consent been granted (Step 2)?");
            }
            const spData = await spRes.json();
            deploymentSpnObjectId = spData.deploymentSpnObjectId;
        } catch (err) {
            appendLog(err.message, "error");
            resetUI();
            return;
        }

        const agentSelect = document.getElementById("agentSelect");
        const selectedAgentSlug = agentSelect ? agentSelect.value : "docgenhybrid";

        let solutionZip = "../Docgen_hybrid_1_0_0_1.zip";
        let connectorZip = "../doc_gen_hybrid_connectors_1_0_0_1_managed.zip";
        let botName = "Skysecure Document Generation Agent";
        let resourceGroupName = `rg-${CUSTOMER_SLUG}-${selectedAgentSlug}`;

        if (selectedAgentSlug === "teamsagent") {
            solutionZip = "../docgen_1_0_0_2.zip";
            connectorZip = "../docgenConnector_1_0_0_2.zip";
            botName = "Skysecure Document Intelligence Agent";
        }

        const payload = {
            agent_slug: selectedAgentSlug,
            customer_slug: CUSTOMER_SLUG,
            customer_tenant_id: tenantId,
            customer_subscription_id: subscriptionId,
            resource_group_name: resourceGroupName,
            agent_image_tag: "v1",
            bot_display_name: botName,
            deployment_spn_object_id_in_customer_tenant: deploymentSpnObjectId,
            // environment_id is intentionally omitted — the orchestrator auto-discovers
            // Power Platform environments after the device-code login step.
            solution_zip_path: solutionZip,
            connector_solution_zip_path: connectorZip,
        };

        try {
            const response = await fetch("/deployments", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail ? JSON.stringify(errorData.detail) : `Server error: ${response.statusText}`);
            }

            const record = await response.json();
            currentDeploymentId = record.deployment_id;
            appendLog(`Deployment queued. ID: ${currentDeploymentId}`, "system");

            pollDeployment(currentDeploymentId);
        } catch (err) {
            appendLog(`Failed to start deployment: ${err.message}`, "error");
            resetUI();
        }
    });

    const authModal = document.getElementById("auth-modal");
    const authModalTitle = document.getElementById("auth-modal-title");
    const deviceCodeSection = document.getElementById("device-code-section");
    const envSelectionSection = document.getElementById("env-selection-section");
    const ppEnvSelect = document.getElementById("pp-env-select");
    const createEnvForm = document.getElementById("create-env-form");
    const newEnvNameInput = document.getElementById("new-env-name");
    const newEnvLocationSelect = document.getElementById("new-env-location");
    const newEnvSkuSelect = document.getElementById("new-env-sku");
    const btnConfirmEnv = document.getElementById("btn-confirm-env");
    const deviceCodeText = document.getElementById("device-code-text");
    const deviceCodeLink = document.getElementById("device-code-link");
    const deviceCodeContext = document.getElementById("device-code-context");
    const copyDeviceCodeBtn = document.getElementById("copy-btn");
    let lastDeviceCodeShown = null;

    if (copyDeviceCodeBtn) {
        copyDeviceCodeBtn.addEventListener("click", () => {
            const code = deviceCodeText.textContent;
            if (code) {
                navigator.clipboard.writeText(code);
                copyDeviceCodeBtn.textContent = "Copied!";
                setTimeout(() => copyDeviceCodeBtn.textContent = "Copy", 2000);
            }
        });
    }

    function validateEnvSelection() {
        if (!btnConfirmEnv || !ppEnvSelect) return;
        if (ppEnvSelect.value === "__CREATE_NEW__") {
            const nameVal = newEnvNameInput ? newEnvNameInput.value.trim() : "";
            btnConfirmEnv.disabled = !nameVal;
        } else {
            btnConfirmEnv.disabled = !ppEnvSelect.value;
        }
    }

    if (ppEnvSelect) {
        ppEnvSelect.addEventListener("change", () => {
            if (ppEnvSelect.value === "__CREATE_NEW__") {
                if (createEnvForm) createEnvForm.classList.remove("hidden");
            } else {
                if (createEnvForm) createEnvForm.classList.add("hidden");
            }
            validateEnvSelection();
        });
    }

    if (newEnvNameInput) {
        newEnvNameInput.addEventListener("input", validateEnvSelection);
    }

    // Wire up the "Continue Deployment" button in the environment selection section
    if (btnConfirmEnv) {
        btnConfirmEnv.addEventListener("click", async () => {
            const selectVal = ppEnvSelect ? ppEnvSelect.value : "";
            if (!selectVal || !currentDeploymentId) return;

            let payload = {};
            if (selectVal === "__CREATE_NEW__") {
                const nameVal = newEnvNameInput ? newEnvNameInput.value.trim() : "";
                const locVal = newEnvLocationSelect ? newEnvLocationSelect.value : "unitedstates";
                const skuVal = newEnvSkuSelect ? newEnvSkuSelect.value : "Production";

                if (!nameVal) {
                    appendLog("Environment name is required for creating a new environment.", "error");
                    return;
                }

                payload = {
                    new_environment: {
                        display_name: nameVal,
                        location: locVal,
                        sku: skuVal
                    }
                };
            } else {
                payload = { instance_url: selectVal };
            }

            btnConfirmEnv.disabled = true;
            btnConfirmEnv.textContent = "Submitting...";
            try {
                const res = await fetch(`/deployments/${currentDeploymentId}/select-environment`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    appendLog(`Failed to submit environment choice: ${err.detail || res.statusText}`, "error");
                    btnConfirmEnv.disabled = false;
                    btnConfirmEnv.textContent = "Continue Deployment";
                } else {
                    if (selectVal === "__CREATE_NEW__") {
                        appendLog("Environment creation submitted. Initiating Stage 1 (shell creation)...", "system");
                    } else {
                        appendLog(`Power Platform environment selected: ${selectVal}`, "system");
                    }
                    authModal.classList.add("hidden");
                }
            } catch (err) {
                appendLog(`Error submitting environment choice: ${err.message}`, "error");
                btnConfirmEnv.disabled = false;
                btnConfirmEnv.textContent = "Continue Deployment";
            }
        });
    }

    let lastSeenStepCount = 0;
    let lastSeenStatus = null;
    let lastSeenStatusMessage = null;

    function pollDeployment(deploymentId) {
        pollTimer = setInterval(async () => {
            try {
                const res = await fetch(`/deployments/${deploymentId}`);
                if (!res.ok) {
                    appendLog(`Failed to poll deployment status (HTTP ${res.status})`, "error");
                    return;
                }
                const record = await res.json();

                if (record.status !== lastSeenStatus) {
                    appendLog(`Status: ${record.status}`, "system");
                    lastSeenStatus = record.status;
                }

                if (record.status_message && record.status_message !== lastSeenStatusMessage) {
                    appendLog(`ℹ ${record.status_message}`, "system");
                    lastSeenStatusMessage = record.status_message;
                }

                const steps = record.steps || [];
                for (let i = lastSeenStepCount; i < steps.length; i++) {
                    const step = steps[i];
                    appendLog(`✓ ${step.step} - ${step.status}`, step.status === "succeeded" ? "success" : "error");
                    if (step.step === "provision_sharepoint" && step.outputs && step.outputs.site_url) {
                        appendLog(`🌐 Provisioned SharePoint Site: ${step.outputs.site_url}`, "success");
                        const spLink = document.getElementById("sharepoint-site-link");
                        if (spLink) {
                            spLink.href = step.outputs.site_url;
                        }
                    }
                }
                lastSeenStepCount = steps.length;

                if (record.status === "awaiting_user_device_auth" && record.device_code_info) {
                    const info = record.device_code_info;
                    const codeKey = `${info.purpose}:${info.user_code}`;
                    if (codeKey !== lastDeviceCodeShown) {
                        // Show device-code section, hide env-selection section
                        if (deviceCodeSection) deviceCodeSection.classList.remove("hidden");
                        if (envSelectionSection) envSelectionSection.classList.add("hidden");
                        if (authModalTitle) authModalTitle.textContent = "Action Required";
                        deviceCodeText.textContent = info.user_code;
                        deviceCodeLink.href = info.verification_uri;
                        if (info.purpose === "pac_auth") {
                            deviceCodeContext.textContent = "PAC CLI needs a one-time login to import the solution.";
                        } else if (info.purpose === "graph_auth") {
                            deviceCodeContext.textContent = "Teams Admin Catalog needs a one-time login to upload the manifest.";
                        } else {
                            deviceCodeContext.textContent = "Power Platform needs a one-time login to create connections.";
                        }
                        authModal.classList.remove("hidden");
                        appendLog(`Waiting for login: ${info.user_code} at ${info.verification_uri}`, "system");
                        lastDeviceCodeShown = codeKey;
                    }
                } else if (record.status === "awaiting_environment_selection" && record.available_pp_environments !== undefined) {
                    // Switch modal to show environment picker + creation option
                    if (deviceCodeSection) deviceCodeSection.classList.add("hidden");
                    if (envSelectionSection) envSelectionSection.classList.remove("hidden");
                    if (authModalTitle) authModalTitle.textContent = "Select or Create Power Platform Environment";

                    // Populate dropdown if not already populated for this deployment ID
                    if (ppEnvSelect && ppEnvSelect.getAttribute("data-dep-id") !== deploymentId) {
                        ppEnvSelect.setAttribute("data-dep-id", deploymentId);
                        const envs = record.available_pp_environments || [];
                        ppEnvSelect.innerHTML = '<option value="">-- Select an environment --</option>' +
                            '<option value="__CREATE_NEW__">+ Create New Environment</option>' +
                            envs.map(e =>
                                `<option value="${e.instanceUrl}">${e.displayName} (${e.environmentSku})</option>`
                            ).join("");

                        if (createEnvForm) createEnvForm.classList.add("hidden");
                        if (newEnvNameInput) newEnvNameInput.value = "";
                        if (btnConfirmEnv) btnConfirmEnv.disabled = true;

                        // Auto-select location dropdown default based on subscription if possible
                        if (newEnvLocationSelect) {
                            const subText = (subscriptionSelect ? subscriptionSelect.value : "").toLowerCase();
                            if (subText.includes("india")) newEnvLocationSelect.value = "india";
                            else if (subText.includes("europe")) newEnvLocationSelect.value = "europe";
                            else if (subText.includes("asia")) newEnvLocationSelect.value = "asia";
                            else if (subText.includes("australia")) newEnvLocationSelect.value = "australia";
                            else if (subText.includes("uk") || subText.includes("unitedkingdom")) newEnvLocationSelect.value = "unitedkingdom";
                            else if (subText.includes("canada")) newEnvLocationSelect.value = "canada";
                            else if (subText.includes("japan")) newEnvLocationSelect.value = "japan";
                            else newEnvLocationSelect.value = "unitedstates";
                        }
                    }
                    authModal.classList.remove("hidden");
                } else if (!authModal.classList.contains("hidden") &&
                           (record.status !== "awaiting_user_device_auth" || !record.device_code_info) &&
                           record.status !== "awaiting_environment_selection") {
                    authModal.classList.add("hidden");
                    if (ppEnvSelect) {
                        ppEnvSelect.removeAttribute("data-dep-id");
                        ppEnvSelect.innerHTML = '<option value="">-- Select an environment --</option>';
                    }
                    if (createEnvForm) createEnvForm.classList.add("hidden");
                    if (btnConfirmEnv) { btnConfirmEnv.disabled = true; btnConfirmEnv.textContent = "Continue Deployment"; }
                }

                if (record.status === "succeeded") {
                    clearInterval(pollTimer);
                    if (record.error) {
                        // Deployment succeeded but teardown failed - surface loudly, still show success modal
                        appendLog(record.error, "warn");
                    }
                    appendLog("Deployment succeeded.", "success");
                    downloadManifestBtn.href = `/api/manifest/${AGENT_SLUG}/${CUSTOMER_SLUG}`;

                    const catalogDetail = document.getElementById("catalog-published-detail");
                    if (catalogDetail) {
                        catalogDetail.textContent = record.catalog_teams_app_id
                            ? `Catalog Teams App ID: ${record.catalog_teams_app_id}`
                            : "Publish status unavailable - check deployment steps below or download the zip and upload manually.";
                    }

                    const teamsAdminBtn = document.getElementById("teams-admin-portal-btn");
                    if (teamsAdminBtn) {
                        teamsAdminBtn.href = "https://admin.teams.microsoft.com/policies/manage-apps";
                    }
                    
                    // Check if sharepoint site URL is in steps
                    const spStep = (record.steps || []).find(s => s.step === "provision_sharepoint");
                    if (spStep && spStep.outputs && spStep.outputs.site_url) {
                        const spLink = document.getElementById("sharepoint-site-link");
                        if (spLink) spLink.href = spStep.outputs.site_url;
                    }

                    setTimeout(() => successModal.classList.remove("hidden"), 1000);
                    resetUI();
                } else if (record.status === "failed") {
                    clearInterval(pollTimer);
                    authModal.classList.add("hidden");
                    appendLog(`Deployment failed: ${record.error || "unknown error"}`, "error");
                    resetUI();
                }
            } catch (err) {
                appendLog(`Polling error: ${err.message}`, "error");
            }
        }, 3000);
    }

    function resetUI() {
        btnText.classList.remove("hidden");
        loader.classList.add("hidden");
        deployBtn.disabled = false;
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        lastSeenStepCount = 0;
        lastSeenStatus = null;
    }
});
