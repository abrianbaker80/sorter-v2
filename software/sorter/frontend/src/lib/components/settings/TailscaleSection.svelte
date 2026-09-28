<script lang="ts">
	import { onMount } from 'svelte';
	import { machineHttpBaseUrlFromWsUrl, getBackendHttpBase } from '$lib/backend';
	import { getMachineContext } from '$lib/machines/context';
	import Modal from '$lib/components/Modal.svelte';
	import { Button, Input, Alert } from '$lib/components/primitives';
	import { Ellipsis, Wifi, WifiOff } from 'lucide-svelte';

	const REPAIR_MESSAGES = {
		restart: 'Tailscale service restarted.',
		install: 'Tailscale installation repair started.',
		logout: 'Previous sign-in cleared. Enter an auth key to connect again.'
	};
	const machine = getMachineContext();
	type RepairAction = keyof typeof REPAIR_MESSAGES;

	type TailscaleStatus = {
		installed: boolean;
		connected: boolean;
		can_repair?: boolean;
		hostname?: string;
		ipv4?: string;
		tailnet?: string;
		error?: string;
		installing?: boolean;
		install_error?: string | null;
	};

	let status = $state<TailscaleStatus | null>(null);
	let loadError = $state<string | null>(null);
	let authKeyDraft = $state('');
	let applying = $state(false);
	let applyError = $state<string | null>(null);
	let applySuccess = $state(false);
	let repair_action = $state<RepairAction | null>(null);
	let repair_error = $state<string | null>(null);
	let repair_message = $state('');
	let menu_open = $state(false);
	let menu_element = $state<HTMLDivElement | null>(null);
	let clear_confirm_open = $state(false);
	let menu_trigger: HTMLButtonElement | null = null;
	const busy = $derived(applying || repair_action !== null || status?.installing === true);

	function httpBase(): string {
		return machineHttpBaseUrlFromWsUrl(machine.machine?.url) ?? getBackendHttpBase();
	}

	async function loadStatus() {
		loadError = null;
		try {
			const res = await fetch(`${httpBase()}/api/tailscale/status`);
			if (!res.ok) throw new Error(await res.text());
			const was_installing = status?.installing;
			status = await res.json();
			if (was_installing && !status?.installing) {
				repair_message = status?.install_error ? '' : 'Tailscale installation repair completed.';
			}
		} catch (e: any) {
			loadError = e.message ?? 'Failed to load Tailscale status';
		}
	}

	async function applyAuthKey() {
		const key = authKeyDraft.trim();
		if (!key || busy) return;
		applying = true;
		applyError = null;
		applySuccess = false;
		repair_error = null;
		repair_message = '';
		try {
			const res = await fetch(`${httpBase()}/api/tailscale/up`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ auth_key: key })
			});
			if (!res.ok) throw new Error(await res.text());
			const data = await res.json();
			if (!data.ok) {
				applyError = data.error ?? 'Failed to apply auth key';
			} else {
				applySuccess = true;
				authKeyDraft = '';
				if (data.status) status = data.status;
			}
		} catch (e: any) {
			applyError = e.message ?? 'Failed to apply auth key';
		} finally {
			applying = false;
		}
	}

	function closeMenu() {
		menu_open = false;
		menu_trigger?.focus();
	}

	function toggleMenu(event: MouseEvent) {
		menu_trigger = event.currentTarget as HTMLButtonElement;
		menu_open = !menu_open;
	}

	function handleOutsideClick(event: MouseEvent) {
		if (menu_open && !menu_element?.contains(event.target as Node)) menu_open = false;
	}

	function handleKeydown(event: KeyboardEvent) {
		if (menu_open && event.key === 'Escape') closeMenu();
	}

	function requestClearSignIn() {
		closeMenu();
		clear_confirm_open = true;
	}

	async function runRepair(action: RepairAction) {
		if (busy) return;
		closeMenu();
		clear_confirm_open = false;
		repair_action = action;
		repair_error = null;
		repair_message = '';
		applyError = null;
		applySuccess = false;
		try {
			const res = await fetch(`${httpBase()}/api/tailscale/${action}`, { method: 'POST' });
			const data = await res.json();
			if (data.status) status = data.status;
			if (!res.ok || !data.ok) {
				throw new Error(data.error ?? data.detail ?? 'Tailscale repair failed');
			}
			repair_message = data.message ?? REPAIR_MESSAGES[action];
		} catch (error: unknown) {
			repair_error =
				error instanceof TypeError && action === 'logout'
					? 'Connection lost while clearing sign-in. Reconnect locally to check the machine and join again.'
					: error instanceof Error
						? error.message
						: 'Tailscale repair failed';
		} finally {
			repair_action = null;
		}
	}

	onMount(() => {
		void loadStatus();
	});

	// The machine installs Tailscale on its own; watch until it's there.
	$effect(() => {
		if (!status || (status.installed && !status.installing)) return;
		const timer = setInterval(() => void loadStatus(), 5000);
		return () => clearInterval(timer);
	});
</script>

<svelte:window onclick={handleOutsideClick} onkeydown={handleKeydown} />

<div class="flex flex-col gap-4">
	<!-- Status row -->
	<div class="border border-border bg-surface px-3 py-3">
		<div class="flex items-center gap-2">
			{#if status?.connected}
				<Wifi size={14} class="text-success" />
				<span class="text-sm font-medium text-text">Connected</span>
				<span class="ml-auto font-mono text-sm text-text-muted">{status.ipv4}</span>
			{:else if status !== null}
				<WifiOff size={14} class="text-text-muted" />
				<span class="text-sm font-medium text-text">
					{status.installed
						? 'Not connected'
						: status.installing
							? 'Installing Tailscale...'
							: 'Tailscale not installed'}
				</span>
			{:else}
				<span class="text-sm text-text-muted">Loading...</span>
			{/if}
			<div bind:this={menu_element} class="relative ml-auto">
				<Button variant="ghost" size="sm" title="Tailscale repair options" onclick={toggleMenu}>
					<Ellipsis size={18} />
					<span class="sr-only">Tailscale repair options</span>
				</Button>
				{#if menu_open}
					<div
						class="absolute top-full right-0 z-20 mt-1 flex w-72 max-w-[calc(100vw-3rem)] flex-col gap-1 border border-border bg-surface p-2 shadow-lg"
						role="group"
						aria-label="Tailscale repair options"
					>
						<div class="flex items-center justify-between gap-2 border-b border-border pb-2">
							<span class="text-sm font-medium">Repair options</span>
							<Button variant="ghost" size="sm" class="text-sm" onclick={closeMenu}>Close</Button>
						</div>
						<Button
							variant="ghost"
							class="justify-start"
							disabled={busy || !status?.installed || !status.can_repair}
							onclick={() => void runRepair('restart')}
						>
							Restart service
						</Button>
						<Button
							variant="ghost"
							class="justify-start"
							disabled={busy || !status?.can_repair}
							onclick={() => void runRepair('install')}
						>
							Install / repair installation
						</Button>
						<Button
							variant="ghost"
							class="justify-start text-danger"
							disabled={busy || !status}
							onclick={requestClearSignIn}
						>
							Clear previous sign-in…
						</Button>
						<p class="px-3 py-1 text-sm text-text-muted">
							Restarting or repairing may briefly interrupt this connection.
						</p>
						{#if status?.can_repair === false}
							<p class="px-3 py-1 text-sm text-text-muted">
								Service repair is not available on this machine.
							</p>
						{/if}
					</div>
				{/if}
			</div>
		</div>
		{#if status?.connected}
			<div class="mt-2 flex items-end justify-between gap-4">
				<div class="flex flex-col gap-0.5">
					<div class="text-sm text-text-muted">
						Hostname: <span class="font-mono text-text">{status.hostname}</span>
					</div>
					{#if status.tailnet}
						<div class="text-sm text-text-muted">
							Tailnet: <span class="font-mono text-text">{status.tailnet}</span>
						</div>
					{/if}
				</div>
			</div>
		{/if}
		{#if status && !status.connected && status.error}
			<div class="mt-1 text-sm text-text-muted">{status.error}</div>
		{/if}
	</div>

	<!-- Warning -->
	<Alert variant="warning">
		Entering an auth key gives the owner of that Tailscale network SSH access to this machine and
		access to your local network. Only use a key you generated yourself or from someone you fully
		trust.
	</Alert>

	<!-- Auth key input -->
	<div>
		<div class="mb-2 text-sm font-medium text-text">Auth Key</div>
		<div class="flex gap-2">
			<Input
				type="password"
				placeholder="tskey-auth-..."
				bind:value={authKeyDraft}
				class="flex-1 font-mono"
			/>
			<Button
				variant="primary"
				size="sm"
				disabled={!authKeyDraft.trim() || busy || (status !== null && !status.installed)}
				loading={applying}
				onclick={() => void applyAuthKey()}
			>
				{applying ? 'Applying...' : 'Apply'}
			</Button>
		</div>
		<div class="mt-1 text-sm text-text-muted">
			Generate an auth key at <span class="font-mono">tailscale.com/admin/settings/keys</span>. The
			machine will join (or switch to) that network immediately.
		</div>
	</div>

	{#if applyError}
		<Alert variant="danger">{applyError}</Alert>
	{/if}
	{#if applySuccess}
		<Alert variant="success">Auth key applied — machine is now connected.</Alert>
	{/if}
	{#if repair_action || status?.installing}
		<Alert variant="info">
			{repair_action === 'restart'
				? 'Restarting Tailscale service…'
				: repair_action === 'install' || status?.installing
					? 'Repairing Tailscale installation…'
					: 'Clearing previous sign-in…'}
		</Alert>
	{/if}
	{#if repair_error}
		<Alert variant="danger">{repair_error}</Alert>
	{/if}
	{#if status?.install_error}
		<Alert variant="danger">
			Installation failed: {status.install_error}. Use Install / repair installation in the repair
			menu to try again.
		</Alert>
	{/if}
	{#if repair_message && !status?.installing}
		<Alert variant="success">{repair_message}</Alert>
	{/if}
	{#if loadError}
		<Alert variant="warning">{loadError}</Alert>
	{/if}
</div>

<Modal bind:open={clear_confirm_open} title="Clear previous Tailscale sign-in?">
	<div class="flex flex-col gap-4">
		<p class="text-sm text-text">
			This disconnects the machine from its Tailscale network and removes any saved setup key. If
			you opened this page through Tailscale, it may stop responding. Open Settings on the machine's
			local network and enter a new auth key here to connect again.
		</p>
		<div class="flex justify-end gap-2">
			<Button variant="secondary" onclick={() => (clear_confirm_open = false)}>Cancel</Button>
			<Button variant="danger" disabled={busy} onclick={() => void runRepair('logout')}>
				Clear sign-in
			</Button>
		</div>
	</div>
</Modal>
