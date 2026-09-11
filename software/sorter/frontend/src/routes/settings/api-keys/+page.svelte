<script lang="ts">
	import ApiKeysSection from '$lib/components/settings/ApiKeysSection.svelte';
	import SectionCard from '$lib/components/settings/SectionCard.svelte';
</script>

<svelte:head><title>Sorter - API Keys</title></svelte:head>

<SectionCard
	title="API Keys"
	description="Configure encrypted provider keys used by detection and Project Harvest."
>
	<ApiKeysSection />
</SectionCard>
