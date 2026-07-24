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

    async function resolveTenantAndSubscriptions() {
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
                fetchSubscriptions(resolvedTenantId);
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
        resolveTimer = setTimeout(resolveTenantAndSubscriptions, 350);
    });

    async function fetchSubscriptions(tenantId) {
        if (!subscriptionSelect) return;
        subscriptionSelect.innerHTML = `<option value="">-- Discovering Subscriptions... --</option>`;
        try {
            const res = await fetch(`/api/azure/subscriptions?tenant_id=${tenantId}`);
            if (res.ok) {
                const subs = await res.json();
                if (subs && subs.length > 0) {
                    subscriptionSelect.innerHTML = `<option value="">-- Select a subscription --</option>` + subs.map(s => 
                        `<option value="${s.subscriptionId}">${s.displayName || s.subscriptionId} (${s.subscriptionId.substring(0, 8)}...)</option>`
                    ).join("");
                    assignRoleSubscriptionIdInput.value = "";
                } else {
                    subscriptionSelect.innerHTML = `<option value="">-- No subscriptions found --</option>`;
                }
            } else {
                subscriptionSelect.innerHTML = `<option value="">-- Complete Admin Consent (Step 2) first --</option>`;
            }
        } catch (err) {
            subscriptionSelect.innerHTML = `<option value="">-- Complete Admin Consent (Step 2) first --</option>`;
        }
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
    // Step 3: Assign Role button
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
        const subId = assignRoleSubscriptionIdInput.value.trim() || (subscriptionSelect ? subscriptionSelect.value : "");
        if (!subId) {
            showAssignRoleAlert("No subscription selected yet. Complete Step 2 first.", "error");
            return;
        }
        btnAssignRole.disabled = true;
        showAssignRoleAlert("Fetching role assignment link...", "info");
        try {
            const res = await fetch(`/api/azure/assign-role-link?subscription_id=${subId}`);
            if (!res.ok) {
                const errorData = await res.json().catch(() => ({}));
                throw new Error(errorData.detail || "Failed to build the role assignment link.");
            }
            const data = await res.json();
            window.open(data.portalUrl, "_blank");
            assignRoleInstructions.textContent = data.instructions;
            assignRoleCliSpan.textContent = data.powershellFallback;
            assignRoleCliBox.classList.remove("hidden");
            showAssignRoleAlert("Portal opened in a new tab. Complete the role assignment there, or use the PowerShell fallback below.", "info");
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
    // Step 4: Resource Group selection & Deployment
    // ---------------------------------------------------------------------
    const resourceGroupSelect = document.getElementById("resourceGroupSelect");
    const newResourceGroupInput = document.getElementById("newResourceGroupInput");
    const btnFetchRgs = document.getElementById("btn-fetch-rgs");

    if (resourceGroupSelect) {
        resourceGroupSelect.addEventListener("change", () => {
            if (resourceGroupSelect.value === "__NEW__") {
                newResourceGroupInput.classList.remove("hidden");
                newResourceGroupInput.required = true;
            } else {
                newResourceGroupInput.classList.add("hidden");
                newResourceGroupInput.required = false;
            }
        });
    }

    async function fetchResourceGroups() {
        const tenantId = tenantIdInput.value.trim();
        const subId = assignRoleSubscriptionIdInput.value.trim();
        if (!tenantId || !subId) {
            alert("Please enter Tenant ID (Step 2) and Subscription ID (Step 3) first.");
            return;
        }
        btnFetchRgs.disabled = true;
        btnFetchRgs.textContent = "Fetching...";
        try {
            const res = await fetch(`/api/azure/resource-groups?tenant_id=${tenantId}&subscription_id=${subId}`);
            if (!res.ok) {
                const errorData = await res.json().catch(() => ({}));
                throw new Error(errorData.detail || "Could not fetch resource groups. Ensure 'Assign Role' step has been completed.");
            }
            const rgs = await res.json();
            resourceGroupSelect.innerHTML = '<option value="__NEW__">+ Create New Resource Group</option>';
            rgs.forEach(rg => {
                const opt = document.createElement("option");
                opt.value = rg;
                opt.textContent = rg;
                resourceGroupSelect.appendChild(opt);
            });
            appendLog(`Loaded ${rgs.length} existing resource group(s) from subscription.`, "system");
        } catch (err) {
            appendLog(`Resource group lookup: ${err.message}`, "warn");
        } finally {
            btnFetchRgs.disabled = false;
            btnFetchRgs.textContent = "Fetch Existing";
        }
    }

    if (btnFetchRgs) {
        btnFetchRgs.addEventListener("click", fetchResourceGroups);
    }

    // ---------------------------------------------------------------------
    // Step 4: Power Platform Environment ID - manual entry only.
    // (Pre-deployment auto-discovery via device code removed; the mid-
    // deployment device-code popup further below, driven by
    // record.status === "awaiting_user_device_auth", is unchanged and is
    // the only device-code flow left in this UI.)
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
            appendLog("Subscription ID is required (Step 3). Complete Step 2 first to discover subscriptions.", "error");
            resetUI();
            return;
        }

        // Determine resource group name (selected existing or newly typed)
        const selectedRgOption = resourceGroupSelect ? resourceGroupSelect.value : "__NEW__";
        const resourceGroupName = selectedRgOption === "__NEW__"
            ? newResourceGroupInput.value.trim()
            : selectedRgOption;

        if (!resourceGroupName) {
            appendLog("Resource Group Name is required.", "error");
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
            power_platform_tenant_id: "547b64a7-e66e-48df-a146-3e898cbcb60f",
            environment_id: document.getElementById("environmentId").value.trim(),
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

    let lastSeenStepCount = 0;
    let lastSeenStatus = null;

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
                        deviceCodeText.textContent = info.user_code;
                        deviceCodeLink.href = info.verification_uri;
                        deviceCodeContext.textContent = info.purpose === "pac_auth"
                            ? "PAC CLI needs a one-time login to import the solution."
                            : "Power Platform needs a one-time login to create connections.";
                        authModal.classList.remove("hidden");
                        appendLog(`Waiting for login: ${info.user_code} at ${info.verification_uri}`, "system");
                        lastDeviceCodeShown = codeKey;
                    }
                } else if (!authModal.classList.contains("hidden")) {
                    authModal.classList.add("hidden");
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
