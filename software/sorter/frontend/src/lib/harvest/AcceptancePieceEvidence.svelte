<script lang="ts">
	import { ExternalLink, ImageOff } from 'lucide-svelte';
	import type { HarvestAcceptancePiece } from './api';
	import type { ImageState } from '$lib/components/records/piece-images';

	let {
		items,
		imageStates,
		loading = false
	}: {
		items: HarvestAcceptancePiece[];
		imageStates: Record<string, ImageState | undefined>;
		loading?: boolean;
	} = $props();

	let groupedItems = $derived.by(() => {
		const groups = new Map<
			string,
			{
				groupId: string;
				groupLabel: string;
				binId: string;
				exception: boolean;
				items: HarvestAcceptancePiece[];
			}
		>();
		for (const item of items) {
			const binId = item.bin_id ?? item.display_bin_id ?? 'Unknown destination';
			const key = `${item.group_id}:${binId}`;
			const existing = groups.get(key);
			if (existing) existing.items.push(item);
			else {
				groups.set(key, {
					groupId: item.group_id,
					groupLabel: item.group_label,
					binId,
					exception: item.exception,
					items: [item]
				});
			}
		}
		return [...groups.values()].sort((a, b) => {
			if (a.exception !== b.exception) return a.exception ? 1 : -1;
			return a.binId.localeCompare(b.binId, undefined, { numeric: true });
		});
	});

	function imageFor(item: HarvestAcceptancePiece): { src: string; label: string } | null {
		const state = imageStates[item.piece_id];
		const captured =
			state?.images.find((image) => image.used && !image.excluded_from_result) ??
			state?.images.find((image) => !image.excluded_from_result) ??
			state?.images[0];
		if (captured) return { src: captured.src, label: 'Captured sort image' };
		const stock = state?.stockUrl ?? item.summary.preview_url;
		return stock ? { src: stock, label: 'Stock reference image' } : null;
	}

	function confidence(value: number | null | undefined): string {
		return typeof value === 'number' ? `${Math.round(value * 100)}% confidence` : 'Confidence unavailable';
	}
</script>

<section class="mt-4 border-t border-border pt-4" aria-label="Pieces sorted in this controlled test">
	<div class="flex flex-wrap items-end justify-between gap-2">
		<div>
			<h3 class="font-semibold text-text">Pieces sorted in this test</h3>
			<p class="mt-1 text-xs text-text-muted">
				Grouped by the destination you should inspect. Captured images are shown before stock references.
			</p>
		</div>
		<div class="text-sm font-semibold text-text">{items.length} piece{items.length === 1 ? '' : 's'}</div>
	</div>

	{#if loading && items.length === 0}
		<div class="mt-3 border border-border bg-bg p-4 text-sm text-text-muted">
			Loading the exact controlled-test pieces…
		</div>
	{:else if items.length === 0}
		<div class="mt-3 border border-border bg-bg p-4 text-sm text-text-muted">
			No confirmed pieces have been recorded for this controlled test yet.
		</div>
	{:else}
		<div class="mt-3 space-y-4">
			{#each groupedItems as group (`${group.groupId}:${group.binId}`)}
				<section class={group.exception ? 'border border-warning/50 bg-warning/5 p-3' : 'border border-border bg-bg p-3'}>
					<div class="flex flex-wrap items-center justify-between gap-2">
						<div>
							<div class="font-semibold text-text">{group.groupLabel}</div>
							<div class="font-mono text-xs text-info">{group.binId}</div>
						</div>
						<div class="text-xs text-text-muted">
							{group.items.length} piece{group.items.length === 1 ? '' : 's'} to verify
						</div>
					</div>

					<div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
						{#each group.items as item (item.allocation_id)}
							{@const displayImage = imageFor(item)}
							<a
								href={`/tracked/${item.piece_id}`}
								class="group block border border-border bg-surface p-2 hover:border-info"
							>
								<div class="flex gap-3">
									<div class="flex h-24 w-24 shrink-0 items-center justify-center border border-border bg-white">
										{#if displayImage}
											<img
												src={displayImage.src}
												alt={`${item.summary.part_name ?? item.summary.part_id ?? 'Sorted piece'} evidence`}
												class="h-full w-full object-contain"
											/>
										{:else}
											<ImageOff class="h-6 w-6 text-text-muted" aria-label="No image available" />
										{/if}
									</div>
									<div class="min-w-0 flex-1">
										<div class="flex items-start justify-between gap-2">
											<div class="font-semibold text-text">
												{item.summary.part_name ?? 'Unidentified piece'}
											</div>
											<ExternalLink class="h-4 w-4 shrink-0 text-text-muted group-hover:text-info" />
										</div>
										<div class="mt-1 text-xs text-text-muted">
											Part {item.summary.part_id ?? 'unknown'} · {item.summary.color_name ?? item.summary.color_id ?? 'unknown color'}
										</div>
										<div class="mt-2 text-xs text-text-muted">{confidence(item.summary.confidence)}</div>
										<div class="mt-1 text-xs text-text-muted">
											{displayImage?.label ?? 'No image available'} · Sorted #{item.sequence}
										</div>
									</div>
								</div>
							</a>
						{/each}
					</div>
				</section>
			{/each}
		</div>
	{/if}
</section>
