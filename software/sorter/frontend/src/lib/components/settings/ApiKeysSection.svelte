<script lang="ts">
	import { onMount } from 'svelte';
	import { getBackendHttpBase, machineHttpBaseUrlFromWsUrl } from '$lib/backend';
	import { getMachineContext } from '$lib/machines/context';
	import { Key, Check, AlertTriangle } from 'lucide-svelte';

	const machine = getMachineContext();

	type Provider = {
		id: string;
		label: string;
		envVar: string;
		placeholder: string;
		description: string;
	};

	const PROVIDERS: Provider[] = [
		{
			id: 'openrouter',
			label: 'OpenRouter',
			envVar: 'OPENROUTER_API_KEY',
			placeholder: 'sk-or-v1-...',
			description: 'Used for cloud-assisted detection.'
		},
		{
			id: 'rebrickable',
			label: 'Rebrickable',
			envVar: 'REBRICKABLE_API_KEY',
			placeholder: 'Rebrickable API key',
			description: 'Used by Project Harvest to fetch and freeze official set inventories.'
		}
	];

	let savedKeys = $state<Record<string, string | null>>({});
	let inputKeys = $state<Record<string, string>>({});
	let saving = $state<Record<string, boolean>>({});
	let statusMsg = $state<string | null>(null);
	let errorMsg = $state<string | null>(null);
	let loading = $state(true);

	function currentBackendBaseUrl(): string {
		return machineHttpBaseUrlFromWsUrl(machine.machine?.url) ?? getBackendHttpBase();
	}

	async function loadKeys() {
		loading = true;
		try {
			const res = await fetch(`${currentBackendBaseUrl()}/api/settings/api-keys`);
			if (!res.ok) throw new Error(await res.text());
			const data = await res.json();
			savedKeys = data.keys ?? {};
		} catch (e: any) {
			errorMsg = e.message ?? 'Failed to load API keys.';
		} finally {
			loading = false;
		}
	}

	async function saveKey(providerId: string) {
		const key = inputKeys[providerId]?.trim();
		if (!key) return;
		saving = { ...saving, [providerId]: true };
		errorMsg = null;
		statusMsg = null;
		try {
			const res = await fetch(`${currentBackendBaseUrl()}/api/settings/api-keys`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ provider: providerId, key })
			});
			if (!res.ok) throw new Error(await res.text());
			const data = await res.json();
			statusMsg = data.message ?? 'Saved.';
			inputKeys[providerId] = '';
			await loadKeys();
		} catch (e: any) {
			errorMsg = e.message ?? 'Failed to save API key.';
		} finally {
			saving = { ...saving, [providerId]: false };
		}
	}

	onMount(() => {
		void loadKeys();
	});
</script>

<div class="grid gap-4">
	{#each PROVIDERS as provider (provider.id)}
		<div class="border border-border bg-surface px-3 py-3">
			<div class="flex items-center gap-2">
				<Key size={14} class="text-text-muted" />
				<span class="text-sm font-medium text-text">{provider.label}</span>
				{#if savedKeys[provider.id]}
					<span class="ml-auto flex items-center gap-1 text-xs text-success dark:text-emerald-400">
						<Check size={12} />
						{savedKeys[provider.id]}
					</span>
				{:else}
					<span class="ml-auto flex items-center gap-1 text-xs text-text-muted">
						<AlertTriangle size={12} />
						Not set
					</span>
				{/if}
			</div>
			<div class="mt-2 flex gap-2">
				<input
					type="password"
					placeholder={provider.placeholder}
					bind:value={inputKeys[provider.id]}
					class="flex-1 border border-border bg-bg px-2 py-1.5 font-mono text-xs text-text"
				/>
				<button
					type="button"
					onclick={() => void saveKey(provider.id)}
					disabled={!inputKeys[provider.id]?.trim() || saving[provider.id]}
					class="border border-border bg-bg px-3 py-1.5 text-xs text-text transition-colors hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
				>
					{saving[provider.id] ? 'Saving...' : 'Save'}
				</button>
			</div>
			<div class="mt-1 text-sm text-text-muted">
				{provider.description} Stored encrypted and activated as
				<code class="font-mono">{provider.envVar}</code>.
			</div>
		</div>
	{/each}

	{#if errorMsg}
		<div
			class="border border-danger bg-danger/10 px-3 py-2 text-sm text-danger dark:border-danger dark:bg-danger/10 dark:text-red-400"
		>
			{errorMsg}
		</div>
	{/if}
	{#if statusMsg}
		<div class="text-sm text-text-muted">{statusMsg}</div>
	{/if}
</div>
