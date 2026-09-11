<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import PieceThumb from '$lib/components/PieceThumb.svelte';
	import { Alert, Button, Input } from '$lib/components/primitives';
	import AcceptancePieceEvidence from '$lib/harvest/AcceptancePieceEvidence.svelte';
	import {
		fetchPieceImageState,
		type ImageState
	} from '$lib/components/records/piece-images';
	import { getMachinesContext } from '$lib/machines/context';
	import { getBackendHttpBase, getBackendWsBase, machineHttpBaseUrlFromWsUrl } from '$lib/backend';
	import {
		createHarvestFreshCopy,
		HarvestApiError,
		type HarvestFreshCopyRequest,
		activateHarvestProject,
		abortHarvestAcceptanceRun,
		clearHarvestBins,
		confirmHarvestAllocation,
		fetchHarvestBinReadiness,
		fetchHarvestAcceptancePieces,
		fetchHarvestBomFromRebrickable,
		fetchHarvestProject,
		fetchHarvestRuntime,
		finishHarvestAcceptanceRun,
		missingPartsCsvUrl,
		planHarvestCapacity,
		proposeHarvestAllocation,
		recordHarvestGreenLight,
		reopenHarvestAcceptanceRunAfterCorrection,
		startHarvestAcceptanceRun,
		deactivateHarvestProject,
		simulateHarvestProject,
		transitionHarvestProject,
		undoHarvestAllocation,
		updateHarvestReview,
		uploadHarvestBom,
		type HarvestBinReadiness,
		type HarvestAcceptancePiece,
		type HarvestBagProgress,
		type HarvestPartProgress,
		type HarvestProject,
		type HarvestRuntimeStatus
	} from '$lib/harvest/api';
	import { fetchLegoColors, type BrickLinkColor } from '$lib/pieces';
	import {
		partCatalog,
		type PartCatalogIdentity,
		type PartCatalogNamespace
	} from '$lib/stores/partCatalog.svelte';
	import {
		ArrowLeft,
		Check,
		CheckCircle2,
		ChevronDown,
		Circle,
		Download,
		ExternalLink,
		FileUp,
		Layers3,
		Play,
		Power,
		RefreshCw,
		Route,
		Search,
		ShieldCheck,
		TriangleAlert
	} from 'lucide-svelte';

	const manager = getMachinesContext();
	const projectId = $derived(String(page.params.projectId));
	const pageIdentity = $derived(JSON.stringify([currentBackendBaseUrl(), projectId]));
	let pageGeneration = 0;
	let copyDialog: HTMLDialogElement;
	type PendingCopy = { payload: HarvestFreshCopyRequest; copyId?: string; code?: string };
	let pendingCopy = $state<PendingCopy | null>(null);
	let copying = $state(false);
	let copyError = $state<string | null>(null);
	let copyStorageReady = $state(false);
	let project = $state<HarvestProject | null>(null);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let success = $state<string | null>(null);
	let openStep = $state(1);
	let advancedOpen = $state(false);
	let expandedBags = $state<Record<string, boolean>>({});
	let legoColors = $state<BrickLinkColor[]>([]);
	let legoColorsBaseUrl = $state<string | null>(null);
	let legoColorsLoadingBaseUrl = $state<string | null>(null);

	let bomInput = $state<HTMLInputElement | null>(null);
	let bomProvider = $state('private_moc');
	let groupActions = $state<Record<string, string>>({});
	let mappingJson = $state('');
	let priority = $state(100);
	let matchPolicy = $state('exact');
	let fallbackMode = $state('bag_plan');
	let nonSortableAction = $state('exclude');
	let initialReview = $state('');
	let capacityAssignments = $state<Record<string, string>>({});
	let editingAssignments = $state(false);

	let acceptanceTestId = $state('');
	let operator = $state('Owner');
	let acceptancePieceCount = $state(10);
	let acceptanceNotes = $state('');
	let acceptanceBinsEmpty = $state(false);
	let acceptanceObservedMatch = $state(false);
	let acceptanceAbortReason = $state('Operator stopped the controlled test');

	let binReadiness = $state<HarvestBinReadiness | null>(null);
	let clearanceScope = $state<'suggested' | 'all'>('suggested');
	let physicalBinsEmptied = $state(false);
	let greenLightReason = $state('');
	let greenLightConfirmed = $state(false);
	let runtimeStatus = $state<HarvestRuntimeStatus | null>(null);
	let acceptancePieces = $state<HarvestAcceptancePiece[]>([]);
	let acceptancePieceImages = $state<Record<string, ImageState | undefined>>({});
	let acceptancePiecesLoading = $state(false);
	let acceptancePiecesError = $state<string | null>(null);
	let loadedAcceptanceRunId = $state<string | null>(null);
	let loadedAcceptancePieceCount = $state(-1);
	let activationReason = $state('Live bag sorting for this approved set');
	let activationConfirmed = $state(false);
	let deactivationReason = $state('Operator stopped the live Harvest run');

	let simulatedPieceId = $state('');
	let simulatedPartId = $state('');
	let simulatedColorId = $state('');
	let undoReason = $state('Correction during guided validation');
	let lifecycleReason = $state('');

	const stages = [
		{ number: 1, label: 'Official inventory' },
		{ number: 2, label: 'Review differences' },
		{ number: 3, label: 'Bin plan' },
		{ number: 4, label: 'Simulation' },
		{ number: 5, label: 'Acceptance' },
		{ number: 6, label: 'Clear bins' },
		{ number: 7, label: 'Green light' },
		{ number: 8, label: 'Live sorting' }
	];

	function currentBackendBaseUrl(): string {
		return (
			machineHttpBaseUrlFromWsUrl(
				manager.selectedMachine?.status === 'connected' ? manager.selectedMachine.url : null
			) ?? getBackendHttpBase()
		);
	}

	function formatInteger(value: number | null | undefined): string {
		return value == null ? '—' : value.toLocaleString();
	}

	function formatDate(value: string | null | undefined): string {
		if (!value) return 'Unknown';
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
	}

	function bagDestination(value: HarvestProject, groupId: string): string {
		const activeAssignment = value.activation?.assignments.find(
			(assignment) => assignment.group_id === groupId
		);
		if (activeAssignment) return activeAssignment.bin_id;
		for (const wave of value.capacity_plan?.waves ?? []) {
			const assignment = wave.assignments.find((candidate) => candidate.group_id === groupId);
			if (assignment) return assignment.bin_id;
		}
		return 'Not assigned';
	}

	function bagPercent(group: HarvestBagProgress): number {
		if (group.required <= 0) return 0;
		return Math.min(100, Math.max(0, Math.round((group.confirmed / group.required) * 100)));
	}

	type HarvestBomItemMetadata = {
		part_id: string;
		color_id?: string;
		description?: string | null;
	};

	type HarvestRuntimeAliasEntry = {
		input: { part_id: string; color_id: string };
		target: { part_id: string; color_id: string };
	};

	type HarvestBomMetadata = {
		namespace?: { part?: string; color?: string };
		items?: HarvestBomItemMetadata[];
		runtime_aliases?: {
			entries?: HarvestRuntimeAliasEntry[];
		};
	};

	function bomMetadata(value: HarvestProject | null): HarvestBomMetadata {
		return (value?.bom ?? {}) as HarvestBomMetadata;
	}

	function catalogNamespaces(
		value: HarvestProject | null
	): { part: PartCatalogNamespace; color: string } | null {
		const namespace = bomMetadata(value).namespace;
		if (
			!namespace ||
			(namespace.part !== 'rebrickable_part_number' &&
				namespace.part !== 'bricklink_item_number') ||
			!namespace.color ||
			namespace.color !== namespace.color.trim()
		) {
			return null;
		}
		return { part: namespace.part, color: namespace.color };
	}

	function catalogIdentity(
		value: HarvestProject | null,
		partId: string,
		colorId: string
	): PartCatalogIdentity | null {
		const namespace = catalogNamespaces(value);
		if (!namespace) return null;
		return {
			part_id: partId,
			part_namespace: namespace.part,
			color_id: colorId,
			color_namespace: namespace.color
		};
	}

	function catalogMetadata(partId: string, colorId: string) {
		const identity = catalogIdentity(project, partId, colorId);
		return identity ? partCatalog.get(currentBackendBaseUrl(), identity) : null;
	}

	const bomItemsByElement = $derived.by(
		() =>
			new Map<string, HarvestBomItemMetadata>(
				(bomMetadata(project).items ?? []).map((item) => [
					partProgressKey(item.part_id, String(item.color_id)),
					item
				])
			)
	);

	const runtimeAliasesByTarget = $derived.by(
		() =>
			new Map<string, HarvestRuntimeAliasEntry>(
				(bomMetadata(project).runtime_aliases?.entries ?? []).map((entry) => [
					partProgressKey(entry.target.part_id, entry.target.color_id),
					entry
				])
			)
	);

	const legoColorsById = $derived.by(
		() => new Map<string, BrickLinkColor>(legoColors.map((color) => [String(color.id), color]))
	);

	function partName(partId: string, colorId: string): string {
		const description = bomItemsByElement
			.get(partProgressKey(partId, colorId))
			?.description?.trim();
		return description || catalogMetadata(partId, colorId)?.name || `Part ${partId}`;
	}

	function colorName(partId: string, colorId: string): string {
		const alias = runtimeAliasesByTarget.get(partProgressKey(partId, colorId));
		const activePalette = legoColorsBaseUrl === currentBackendBaseUrl() ? legoColorsById : null;
		const paletteColor = alias
			? activePalette?.get(alias.input.color_id)
			: bomMetadata(project).namespace?.color === 'bricklink_color_id'
				? activePalette?.get(colorId)
				: undefined;
		return paletteColor?.name || `Color ${colorId}`;
	}

	async function loadLegoColors(): Promise<void> {
		const baseUrl = currentBackendBaseUrl();
		if (legoColorsBaseUrl === baseUrl || legoColorsLoadingBaseUrl === baseUrl) return;

		legoColorsLoadingBaseUrl = baseUrl;
		try {
			const colors = await fetchLegoColors(baseUrl);
			if (currentBackendBaseUrl() === baseUrl) {
				legoColors = colors;
				legoColorsBaseUrl = baseUrl;
			}
		} catch {
			// Keep the operator-facing fallback when the existing cached palette is unavailable.
		} finally {
			if (legoColorsLoadingBaseUrl === baseUrl) legoColorsLoadingBaseUrl = null;
		}
	}

	function partImageUrl(partId: string, colorId: string): string | null {
		return catalogMetadata(partId, colorId)?.image_url ?? null;
	}

	type EffectiveGroupPart = Pick<HarvestPartProgress, 'part_id' | 'color_id'> & {
		quantity: number;
	};

	function partProgressKey(partId: string, colorId: string): string {
		return `${partId}\u0000${colorId}`;
	}

	function bagPartRows(value: HarvestProject, group: HarvestBagProgress): HarvestPartProgress[] {
		if (Array.isArray(group.parts)) return group.parts;

		const missingByElement = new Map(
			value.progress.missing
				.filter((part) => part.group_id === group.group_id)
				.map((part) => [partProgressKey(part.part_id, part.color_id), part])
		);
		const effectiveGroup = (
			value.effective_groups as Array<
				HarvestProject['effective_groups'][number] & { parts?: EffectiveGroupPart[] }
			>
		).find((candidate) => candidate.id === group.group_id);

		if (!Array.isArray(effectiveGroup?.parts)) return [...missingByElement.values()];
		return effectiveGroup.parts.map((part) => {
			const progress = missingByElement.get(partProgressKey(part.part_id, part.color_id));
			const required = Number(part.quantity);
			return {
				part_id: part.part_id,
				color_id: part.color_id,
				required,
				confirmed: progress?.confirmed ?? required,
				missing: progress?.missing ?? 0
			};
		});
	}

	function resolveBagCatalog(value: HarvestProject, parts: HarvestPartProgress[]): void {
		const namespace = catalogNamespaces(value);
		if (!namespace || parts.length === 0) return;
		const identities: PartCatalogIdentity[] = parts.map((part) => ({
			part_id: part.part_id,
			part_namespace: namespace.part,
			color_id: part.color_id,
			color_namespace: namespace.color
		}));
		void partCatalog.resolveMany(currentBackendBaseUrl(), identities).catch(() => {});
	}

	function resolveExpandedBagCatalog(value: HarvestProject): void {
		for (const group of value.progress.groups) {
			if (expandedBags[group.group_id]) {
				resolveBagCatalog(value, bagPartRows(value, group));
			}
		}
	}

	function toggleBag(groupId: string, parts: HarvestPartProgress[]): void {
		const expanded = !expandedBags[groupId];
		expandedBags = { ...expandedBags, [groupId]: expanded };
		if (!expanded) return;
		void loadLegoColors();
		if (project) resolveBagCatalog(project, parts);
	}

	function pageGuard() {
		const identity = pageIdentity;
		const generation = pageGeneration;
		return () => identity === pageIdentity && generation === pageGeneration;
	}

	function copyStorageKey(identity = pageIdentity) {
		return `harvest-fresh-copy:v1:${identity}`;
	}

	function newCopyRequestId(): string {
		try {
			if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
			const bytes = crypto.getRandomValues(new Uint8Array(16));
			bytes[6] = (bytes[6] & 0x0f) | 0x40;
			bytes[8] = (bytes[8] & 0x3f) | 0x80;
			const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
			return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
		} catch {
			throw new Error('Secure random generation is unavailable. A copy request could not be created.');
		}
	}

	function isCopyObject(value: unknown): value is Record<string, unknown> {
		return typeof value === 'object' && value !== null && !Array.isArray(value);
	}

	function readCopyRequest(storageKey: string): PendingCopy | null {
		const saved = localStorage.getItem(storageKey);
		if (saved === null) return null;
		const value: unknown = JSON.parse(saved);
		if (!isCopyObject(value) || !isCopyObject(value.payload)) throw new Error('Invalid saved copy request');
		const payload = value.payload;
		if (Object.keys(value).some(key => !['payload', 'copyId', 'code'].includes(key)) ||
			Object.keys(payload).some(key => !['request_id', 'expected_revision', 'operator'].includes(key)) ||
			typeof payload.request_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(payload.request_id) ||
			typeof payload.expected_revision !== 'number' || !Number.isSafeInteger(payload.expected_revision) || payload.expected_revision < 1 ||
			typeof payload.operator !== 'string' || !payload.operator.trim() ||
			Array.from(payload.operator.trim()).length > 120 || /[\x00-\x1f\x7f]/.test(payload.operator.trim()) ||
			('copyId' in value && (typeof value.copyId !== 'string' || !/^harvest-[0-9a-f]{32}$/.test(value.copyId))) ||
			('code' in value && (typeof value.code !== 'string' || !['STALE_PROJECT_REVISION', 'COPY_REQUEST_CONFLICT'].includes(value.code)))) {
			throw new Error('Invalid saved copy request');
		}
		return value as PendingCopy;
	}

	function sameCopyRequest(left: PendingCopy, right: PendingCopy): boolean {
		return left.payload.request_id === right.payload.request_id &&
			left.payload.expected_revision === right.payload.expected_revision &&
			left.payload.operator === right.payload.operator;
	}

	function ownedCopyRequest(storageKey: string, request: PendingCopy): PendingCopy {
		// The captured key binds the backend and source. Check and write synchronously;
		// this protects this page's completions, not transactions across browser tabs.
		const saved = readCopyRequest(storageKey);
		if (!saved || !sameCopyRequest(saved, request)) throw new Error('Saved copy operation changed');
		return saved;
	}

	function copyStorageFailure() {
		copyStorageReady = false;
		copyError = 'Cannot safely read or update the saved copy request. Check browser storage and reload before continuing.';
	}

	function restoreCopyRequest() {
		try {
			pendingCopy = readCopyRequest(copyStorageKey());
			copyStorageReady = true;
		} catch {
			copyStorageFailure();
		}
	}

	async function openCopy(copyId: string) {
		const current = pageGuard();
		const request = pendingCopy;
		try {
			await goto(`/dashboard/harvest/projects/${encodeURIComponent(copyId)}`);
		} catch {
			if (current() && request && pendingCopy && sameCopyRequest(request, pendingCopy)) {
				copyError = 'The copy was created, but navigation failed. Use Open created copy to try again.';
			}
		}
	}

	async function submitFreshCopy() {
		if (!project || copying || busy || !copyStorageReady || pendingCopy?.code) return;
		const current = pageGuard();
		const baseUrl = currentBackendBaseUrl();
		const sourceId = projectId;
		const storageKey = copyStorageKey();
		copying = true;
		copyError = null;
		let request: PendingCopy | null = null;
		try {
			let saved: PendingCopy | null;
			try {
				saved = pendingCopy ? ownedCopyRequest(storageKey, pendingCopy) : readCopyRequest(storageKey);
				if (!pendingCopy && saved) throw new Error('Saved copy operation changed');
			} catch { copyStorageFailure(); return; }
			if (saved?.copyId || saved?.code) {
				pendingCopy = saved;
				if (saved.copyId) await openCopy(saved.copyId);
				return;
			}
			request = saved ?? { payload: {
				request_id: newCopyRequestId(), expected_revision: project.revision, operator: operator.trim()
			} };
			// Persist before sending; a failed write must never create an untracked copy.
			try { localStorage.setItem(storageKey, JSON.stringify(request)); }
			catch { copyStorageFailure(); return; }
			pendingCopy = request;
			const result = await createHarvestFreshCopy(baseUrl, sourceId, request.payload);
			let receipt: PendingCopy;
			try {
				const owner = ownedCopyRequest(storageKey, request);
				receipt = owner.copyId ? owner : { payload: owner.payload, copyId: result.project_id };
				// Retain the returned destination in memory even if writing its receipt fails.
				if (current()) pendingCopy = receipt;
				localStorage.setItem(storageKey, JSON.stringify(receipt));
			} catch { if (current()) copyStorageFailure(); return; }
			if (current() && receipt.copyId) await openCopy(receipt.copyId);
		} catch (e: unknown) {
			if (!current()) return;
			if (request && e instanceof HarvestApiError &&
				['STALE_PROJECT_REVISION', 'COPY_REQUEST_CONFLICT'].includes(e.code ?? '')) {
				try {
					const owner = ownedCopyRequest(storageKey, request);
					pendingCopy = owner.copyId ? owner : { ...owner, code: e.code };
					localStorage.setItem(storageKey, JSON.stringify(pendingCopy));
				} catch { copyStorageFailure(); return; }
			}
			copyError = e instanceof Error ? e.message : 'Copy response unavailable.';
		} finally {
			if (current()) copying = false;
		}
	}

	async function prepareAnotherCopy() {
		const current = pageGuard();
		const request = pendingCopy;
		const storageKey = copyStorageKey();
		if (!request) return;
		if (request.code === 'STALE_PROJECT_REVISION' && !(await loadProject())) return;
		if (!current()) return;
		try {
			const owner = ownedCopyRequest(storageKey, request);
			if (!request.copyId && (owner.copyId || owner.code !== request.code)) {
				pendingCopy = owner;
				return;
			}
			if (request.copyId && owner.copyId && request.copyId !== owner.copyId) {
				throw new Error('Saved copy destination changed');
			}
			localStorage.removeItem(storageKey);
			pendingCopy = null;
			copyStorageReady = true;
			copyError = null;
		} catch {
			copyStorageFailure();
		}
	}

	function resetProjectState() {
		project = null;
		busy = false;
		error = success = null;
		openStep = 1;
		advancedOpen = editingAssignments = false;
		expandedBags = {};
		bomProvider = 'private_moc';
		groupActions = capacityAssignments = {};
		mappingJson = initialReview = '';
		priority = 100;
		matchPolicy = 'exact';
		fallbackMode = 'bag_plan';
		nonSortableAction = 'exclude';
		acceptanceTestId = acceptanceNotes = '';
		acceptancePieceCount = 10;
		acceptanceAbortReason = 'Operator stopped the controlled test';
		acceptanceBinsEmpty = acceptanceObservedMatch = physicalBinsEmptied = greenLightConfirmed = activationConfirmed = false;
		clearanceScope = 'suggested';
		greenLightReason = lifecycleReason = simulatedPieceId = simulatedPartId = simulatedColorId = '';
		activationReason = 'Live bag sorting for this approved set';
		deactivationReason = 'Operator stopped the live Harvest run';
		undoReason = 'Correction during guided validation';
		binReadiness = runtimeStatus = null;
		acceptancePieces = [];
		acceptancePieceImages = {};
		acceptancePiecesLoading = false;
		acceptancePiecesError = loadedAcceptanceRunId = null;
		loadedAcceptancePieceCount = -1;
		copyDialog?.close();
		pendingCopy = copyError = null;
		copying = copyStorageReady = false;
		restoreCopyRequest();
	}

	function reviewValue(): string {
		return JSON.stringify({
			groupActions,
			mappingJson,
			priority,
			matchPolicy,
			fallbackMode,
			nonSortableAction
		});
	}

	function reviewDirty(): boolean {
		return reviewValue() !== initialReview;
	}

	function syncProject(next: HarvestProject, preserveStep = false) {
		project = next;
		groupActions = { ...next.policy.group_actions };
		mappingJson = JSON.stringify(next.mappings, null, 2);
		priority = next.priority;
		matchPolicy = next.match_policy;
		fallbackMode = next.fallback_mode;
		nonSortableAction = next.policy.non_sortable_action;
		capacityAssignments = Object.fromEntries(
			(next.capacity_plan?.waves ?? []).flatMap((wave) =>
				wave.assignments.map((assignment) => [assignment.group_id, assignment.bin_id])
			)
		);
		if (!acceptanceTestId.trim()) acceptanceTestId = `${next.set_number}-acceptance-1`;
		initialReview = reviewValue();
		if (!preserveStep) openStep = nextStepNumber(next);
		resolveExpandedBagCatalog(next);
	}

	function nextStepNumber(value: HarvestProject): number {
		const gates = value.readiness.gates;
		if (gates.live_activation_active || value.state === 'completed') return 8;
		if (gates.green_light_recorded) return 8;
		if (!gates.authoritative_bom_frozen || !gates.runtime_identifiers_ready) return 1;
		if (!gates.review_policies_resolved || !gates.bom_reconciled) return 2;
		if (!gates.capacity_plan_ready) return 3;
		if (!gates.simulation_passed) return 4;
		if (!gates.physical_acceptance_passed) return 5;
		if (!binReadiness || binReadiness.status !== 'clear') return 6;
		return 7;
	}

	function stageComplete(number: number): boolean {
		if (!project) return false;
		const gates = project.readiness.gates;
		if (number === 1)
			return Boolean(gates.authoritative_bom_frozen && gates.runtime_identifiers_ready);
		if (number === 2) return Boolean(gates.review_policies_resolved && gates.bom_reconciled);
		if (number === 3) return Boolean(gates.capacity_plan_ready);
		if (number === 4) return Boolean(gates.simulation_passed);
		if (number === 5) return Boolean(gates.physical_acceptance_passed);
		if (number === 6) return Boolean(gates.green_light_recorded || binReadiness?.status === 'clear');
		if (number === 7) return Boolean(gates.green_light_recorded);
		return Boolean(gates.live_activation_active || project.state === 'completed');
	}

	function nextAction(): { title: string; detail: string; step: number } {
		if (!project) return { title: 'Loading project', detail: '', step: 1 };
		const step = nextStepNumber(project);
		const copy: Record<number, { title: string; detail: string }> = {
			1: {
				title: 'Confirm the official set inventory',
				detail: 'Fetch the Rebrickable inventory and freeze it as immutable evidence.'
			},
			2: {
				title: 'Resolve only the differences',
				detail: 'Matched bags need no work. Review any exceptions, then save the decisions.'
			},
			3: {
				title: 'Build the draft bin plan',
				detail: 'Planning assumes enabled bins are empty and does not change the live sorter.'
			},
			4: {
				title: 'Run the full simulation',
				detail: 'Verify that every required quantity has a destination and fits the planned waves.'
			},
			5: {
				title: 'Run the controlled physical test',
				detail:
					'Arm a limited routing session, feed the test pieces, then inspect the auto-paused results.'
			},
			6: {
				title: 'Verify and clear the planned bins',
				detail: 'Pause the sorter, physically empty the bins, then mark their recorded state clear.'
			},
			7: project.readiness.green_light_approved
				? {
						title: 'Green light recorded',
						detail:
							'The project is approved and ready for explicit paused-state activation.'
					}
				: {
						title: 'Record the operator green light',
						detail: 'Recheck the clear bins, attest to the physical state, and approve deployment.'
					},
			8: project.readiness.gates.live_activation_active
				? {
						title: 'Live bag sorting is active',
						detail: 'Use the global sorter control to resume motion. Harvest will pause automatically when the official quantities are complete.'
					}
				: project.state === 'completed'
					? {
							title: 'Live bag sorting is complete',
							detail: 'The official set quantities are confirmed and the sorter has been paused.'
						}
					: {
							title: 'Activate live bag sorting',
							detail: 'While paused, bind this exact green-lit revision to its planned bag and exception bins.'
						}
		};
		return { ...copy[step], step };
	}

	function friendlyGroup(groupId: string): string {
		const match = groupId.match(/(\d+)$/);
		return match ? `Bag ${Number(match[1])}` : groupId.replaceAll('-', ' ');
	}

	function selectedClearanceBins(): HarvestBinReadiness['suggested_bins_requiring_clearance'] {
		if (!binReadiness) return [];
		return clearanceScope === 'suggested'
			? binReadiness.suggested_bins_requiring_clearance
			: binReadiness.all_bins_requiring_clearance;
	}

	function activeAcceptance() {
		const active = runtimeStatus?.active;
		return active?.project_id === projectId && active.runtime_mode === 'acceptance' ? active : null;
	}

	function activeLiveRuntime() {
		const active = runtimeStatus?.active;
		return active?.project_id === projectId && (active.runtime_mode ?? 'live') === 'live'
			? active
			: null;
	}

	function liveRunBagCount(): number {
		const active = activeLiveRuntime();
		return Number(
			active?.runtime_progress?.bag_confirmed_piece_count ??
				project?.progress.summary.confirmed_quantity ??
				0
		);
	}

	function liveRunExceptionCount(): number {
		return Number(activeLiveRuntime()?.runtime_progress?.exception_confirmed_piece_count ?? 0);
	}

	function liveRunExceptionRate(): number {
		const total = liveRunBagCount() + liveRunExceptionCount();
		return total > 0 ? (liveRunExceptionCount() / total) * 100 : 0;
	}

	function acceptanceReplacementPieceCount(): number {
		const active = activeAcceptance();
		if (!active || active.status !== 'awaiting_observation') return 0;
		const confirmed = Number(active.runtime_progress?.confirmed_piece_count ?? 0);
		const limit = Number(active.test_piece_limit ?? 0);
		return Math.max(0, limit - confirmed);
	}

	function acceptanceEvidenceRunId(): string | null {
		const active = activeAcceptance();
		if (active) return active.activation_id;
		const runId = project?.acceptance?.acceptance_run_id;
		return typeof runId === 'string' && runId ? runId : null;
	}

	function expectedAcceptancePieceCount(): number {
		const active = activeAcceptance();
		if (active) return Number(active.runtime_progress?.confirmed_piece_count ?? 0);
		const count = project?.acceptance?.controlled_piece_count;
		return typeof count === 'number' ? count : Number(count ?? 0);
	}

	async function loadAcceptanceEvidence(runId: string, expectedCount: number) {
		const currentPage = pageGuard();
		acceptancePiecesLoading = true;
		acceptancePiecesError = null;
		try {
			const response = await fetchHarvestAcceptancePieces(
				currentBackendBaseUrl(),
				projectId,
				runId
			);
			if (!currentPage() || response.acceptance_run_id !== runId) return;
			acceptancePieces = response.items;
			loadedAcceptanceRunId = runId;
			loadedAcceptancePieceCount = expectedCount;
			acceptancePieceImages = Object.fromEntries(
				response.items.map((item) => [item.piece_id, { status: 'loading', images: [] }])
			);
			const imageEntries = await Promise.all(
				response.items.map(async (item) => [
					item.piece_id,
					await fetchPieceImageState(
						currentBackendBaseUrl(),
						item.piece_id,
						item.summary.seen_at ?? null
					)
				] as const)
			);
			if (currentPage() && loadedAcceptanceRunId === runId) {
				acceptancePieceImages = Object.fromEntries(imageEntries);
			}
		} catch (e: unknown) {
			if (!currentPage()) return;
			acceptancePiecesError =
				e instanceof Error ? e.message : 'Failed to load controlled-test piece evidence';
		} finally {
			if (!currentPage()) return;
			acceptancePiecesLoading = false;
		}
	}

	function eventLabel(kind: string): string {
		const labels: Record<string, string> = {
			project_created: 'Project created',
			bom_revision_selected: 'Official inventory frozen',
			review_updated: 'Review decisions saved',
			capacity_planned: 'Bin plan generated',
			simulation_completed: 'Simulation completed',
			physical_acceptance_recorded: 'Controlled acceptance recorded',
			physical_acceptance_test_armed: 'Controlled acceptance armed',
			acceptance_allocation_reserved: 'Controlled piece reserved',
			acceptance_allocation_confirmed: 'Controlled piece confirmed',
			physical_acceptance_routing_completed: 'Controlled routing completed',
			physical_acceptance_test_aborted: 'Controlled acceptance stopped',
			physical_acceptance_reopened_after_correction: 'Controlled acceptance reopened after correction',
			bin_clearance_recorded: 'Bin clearance recorded',
			green_light_recorded: 'Deployment green light recorded',
			live_activation_started: 'Live bag sorting activated',
			live_allocation_reserved: 'Physical piece reserved',
			live_allocation_confirmed: 'Physical piece confirmed',
			live_activation_stopped: 'Live bag sorting stopped',
			live_sort_completed: 'Live bag sorting completed',
			state_transitioned: 'Project state changed'
		};
		return labels[kind] ?? kind.replaceAll('_', ' ');
	}

	async function loadProject() {
		const currentPage = pageGuard();
		loading = true;
		error = null;
		try {
			const loaded = await fetchHarvestProject(currentBackendBaseUrl(), projectId);
			if (!currentPage() || !(await refreshRuntime())) return false;
			syncProject(loaded);
			if (loaded.readiness.gates.simulation_passed) {
				const bins = await fetchHarvestBinReadiness(currentBackendBaseUrl(), projectId);
				if (!currentPage()) return false;
				binReadiness = bins;
				openStep = nextStepNumber(loaded);
			}
			return true;
		} catch (e: unknown) {
			if (!currentPage()) return;
			error = e instanceof Error ? e.message : 'Failed to load the Harvest project';
		} finally {
			if (!currentPage()) return;
			loading = false;
		}
	}

	async function refreshRuntime() {
		const currentPage = pageGuard();
		const next = await fetchHarvestRuntime(currentBackendBaseUrl());
		if (!currentPage()) return false;
		runtimeStatus = next;
		return true;
	}

	async function runAction(action: () => Promise<HarvestProject>, message: string) {
		const currentPage = pageGuard();
		busy = true;
		error = null;
		success = null;
		try {
			const next = await action();
			if (!currentPage()) return false;
			syncProject(next);
			success = message;
			return true;
		} catch (e: unknown) {
			if (!currentPage()) return;
			error = e instanceof Error ? e.message : 'The Harvest action failed';
		} finally {
			if (!currentPage()) return;
			busy = false;
		}
	}

	async function fetchOfficialBom() {
		await runAction(
			() => fetchHarvestBomFromRebrickable(currentBackendBaseUrl(), projectId),
			'Official Rebrickable inventory fetched and frozen.'
		);
	}

	async function handleBomUpload(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		await runAction(
			() => uploadHarvestBom(currentBackendBaseUrl(), projectId, file, bomProvider),
			'Uploaded inventory frozen as a new revision.'
		);
	}

	function applyRecommendedReview() {
		groupActions = Object.fromEntries(Object.keys(groupActions).map((key) => [key, 'separate']));
		matchPolicy = 'exact';
		fallbackMode = 'bag_plan';
		nonSortableAction = 'exclude';
	}

	async function saveReview() {
		if (!project || !reviewDirty()) return;
		const current = project;
		const invalidates = Boolean(current.capacity_plan || current.simulation || current.acceptance);
		if (
			invalidates &&
			!window.confirm(
				'These routing decisions changed. Saving will invalidate the existing bin plan, simulation, acceptance, and green light. Continue?'
			)
		) {
			return;
		}
		await runAction(
			() =>
				updateHarvestReview(currentBackendBaseUrl(), projectId, {
					policy: {
						...current.policy,
						group_actions: groupActions,
						non_sortable_action: nonSortableAction
					},
					mappings: JSON.parse(mappingJson) as Record<string, unknown>,
					priority,
					match_policy: matchPolicy,
					fallback_mode: fallbackMode
				}),
			'Review decisions saved.'
		);
	}

	async function planCapacity(useCustom = false) {
		if (!project) return;
		const overrides = useCustom
			? (project.capacity_plan?.waves ?? []).flatMap((wave) =>
					wave.assignments.map((item) => ({
						group_id: item.group_id,
						bin_id: capacityAssignments[item.group_id] ?? item.bin_id
					}))
				)
			: undefined;
		if (!(await runAction(
			() => planHarvestCapacity(currentBackendBaseUrl(), projectId, overrides),
			useCustom ? 'Custom draft assignments saved.' : 'Assumed-empty bin plan generated.'
		))) return;
		editingAssignments = false;
		binReadiness = null;
	}

	async function runSimulation() {
		if (!(await runAction(
			() => simulateHarvestProject(currentBackendBaseUrl(), projectId),
			'Complete quantity-aware simulation passed.'
		))) return;
		binReadiness = null;
	}

	async function armAcceptance() {
		if (!project || !binReadiness) return;
		const current = project;
		const currentBins = binReadiness;
		if (!(await runAction(
			() =>
				startHarvestAcceptanceRun(currentBackendBaseUrl(), projectId, {
					expected_revision: current.revision,
					expected_bin_state_token: currentBins.bin_state_token,
					test_id: acceptanceTestId,
					operator,
					test_piece_limit: acceptancePieceCount,
					physical_bins_verified_empty: acceptanceBinsEmpty
				}),
			'Controlled test armed. Use Resume in the global header, then feed only the configured number of test pieces.'
		))) return;
		if (!(await refreshRuntime())) return;
		acceptanceBinsEmpty = false;
		openStep = 5;
	}

	async function finishAcceptance(result: 'passed' | 'failed') {
		const active = activeAcceptance();
		if (!active) return;
		if (!(await runAction(
			() =>
				finishHarvestAcceptanceRun(currentBackendBaseUrl(), projectId, {
					acceptance_run_id: active.activation_id,
					operator,
					result,
					observed_destinations_match: acceptanceObservedMatch,
					notes: acceptanceNotes
				}),
			`Controlled physical acceptance recorded as ${result}.`
		))) return;
		if (!(await refreshRuntime())) return;
		acceptanceObservedMatch = false;
		if (result === 'passed') await checkBins();
		else openStep = 5;
	}

	async function reopenAcceptanceAfterCorrection() {
		const active = activeAcceptance();
		const replacements = acceptanceReplacementPieceCount();
		if (!active || replacements < 1) return;
		if (!(await runAction(
			() =>
				reopenHarvestAcceptanceRunAfterCorrection(currentBackendBaseUrl(), projectId, {
					acceptance_run_id: active.activation_id,
					operator,
					reason: 'Continue controlled test after audited acceptance allocations were undone.'
				}),
			`Controlled test reopened. Resume the sorter and feed exactly ${replacements} replacement piece${replacements === 1 ? '' : 's'}.`
		))) return;
		if (!(await refreshRuntime())) return;
		openStep = 5;
	}

	async function abortAcceptance() {
		const active = activeAcceptance();
		if (!active) return;
		if (!(await runAction(
			() =>
				abortHarvestAcceptanceRun(currentBackendBaseUrl(), projectId, {
					acceptance_run_id: active.activation_id,
					operator,
					reason: acceptanceAbortReason
				}),
			'Controlled test stopped safely. The recorded test allocations remain in the audit history.'
		))) return;
		if (!(await refreshRuntime())) return;
		acceptanceBinsEmpty = false;
		openStep = 5;
	}

	async function checkBins() {
		const currentPage = pageGuard();
		busy = true;
		error = null;
		try {
			const bins = await fetchHarvestBinReadiness(currentBackendBaseUrl(), projectId);
			if (!currentPage()) return;
			binReadiness = bins;
			openStep = binReadiness.status === 'clear' ? 7 : 6;
			success =
				binReadiness.status === 'clear'
					? 'All planned bins are clear in recorded state.'
					: 'Recorded state checked. Physically empty the listed bins before marking them clear.';
		} catch (e: unknown) {
			if (!currentPage()) return;
			error = e instanceof Error ? e.message : 'Failed to check recorded bin state';
		} finally {
			if (!currentPage()) return;
			busy = false;
		}
	}

	async function clearSelectedBins() {
		const currentPage = pageGuard();
		if (!project || !binReadiness) return;
		busy = true;
		error = null;
		success = null;
		try {
			const response = await clearHarvestBins(currentBackendBaseUrl(), projectId, {
				bin_ids: selectedClearanceBins().map((bin) => bin.bin_id),
				expected_bin_state_token: binReadiness.bin_state_token,
				operator,
				physical_bins_emptied: physicalBinsEmptied
			});
			if (!currentPage()) return;
			binReadiness = response.bin_readiness;
			syncProject(response.project, true);
			physicalBinsEmptied = false;
			openStep = response.bin_readiness.status === 'clear' ? 7 : 6;
			success = String(response.clearance.message ?? 'Selected bins marked clear.');
		} catch (e: unknown) {
			if (!currentPage()) return;
			error = e instanceof Error ? e.message : 'Failed to mark the selected bins clear';
		} finally {
			if (!currentPage()) return;
			busy = false;
		}
	}

	async function greenLight() {
		if (!project || !binReadiness) return;
		const current = project;
		const currentBinReadiness = binReadiness;
		if (!(await runAction(
			() =>
				recordHarvestGreenLight(currentBackendBaseUrl(), projectId, {
					expected_revision: current.revision,
					expected_bin_state_token: currentBinReadiness.bin_state_token,
					operator,
					reason: greenLightReason,
					physical_bins_verified_empty: greenLightConfirmed
				}),
			'Deployment green light recorded. The paused sorter can now be activated for this exact plan.'
		))) return;
		openStep = 8;
	}

	async function activateLiveSorting() {
		if (!project || !binReadiness) return;
		const current = project;
		const currentBins = binReadiness;
		if (!(await runAction(
			() =>
				activateHarvestProject(currentBackendBaseUrl(), projectId, {
					expected_revision: current.revision,
					expected_bin_state_token: currentBins.bin_state_token,
					operator,
					reason: activationReason
				}),
			'Live Harvest bag routing activated. Resume the sorter from the global header when ready.'
		))) return;
		if (!(await refreshRuntime())) return;
		activationConfirmed = false;
		openStep = 8;
	}

	async function stopLiveSorting() {
		if (!(await runAction(
			() =>
				deactivateHarvestProject(currentBackendBaseUrl(), projectId, {
					operator,
					reason: deactivationReason
				}),
			'Live Harvest routing stopped. Bag contents and audit history were preserved.'
		))) return;
		if (!(await refreshRuntime())) return;
	}

	async function simulateLedgerPiece() {
		const currentPage = pageGuard();
		if (!project) return;
		busy = true;
		error = null;
		try {
			const allocation = await proposeHarvestAllocation(currentBackendBaseUrl(), projectId, {
				piece_id: simulatedPieceId,
				part_id: simulatedPartId,
				color_id: simulatedColorId
			});
			if (!currentPage()) return;
			await confirmHarvestAllocation(
				currentBackendBaseUrl(),
				projectId,
				String(allocation.allocation_id)
			);
			if (!currentPage()) return;
			const next = await fetchHarvestProject(currentBackendBaseUrl(), projectId);
			if (!currentPage()) return;
			syncProject(next, true);
			simulatedPieceId = '';
			success = `Simulated piece confirmed in ${String(allocation.group_id)}.`;
		} catch (e: unknown) {
			if (!currentPage()) return;
			error = e instanceof Error ? e.message : 'Failed to confirm the simulated piece';
		} finally {
			if (!currentPage()) return;
			busy = false;
		}
	}

	async function undoAllocation(allocationId: string) {
		const currentPage = pageGuard();
		await undoHarvestAllocation(currentBackendBaseUrl(), projectId, allocationId, undoReason);
		if (!currentPage()) return;
		const next = await fetchHarvestProject(currentBackendBaseUrl(), projectId);
		if (!currentPage()) return;
		syncProject(next, true);
		if (!(await refreshRuntime())) return;
		success = 'Allocation undone with an audit reason.';
	}

	async function transition(target: 'review' | 'paused' | 'completed') {
		if (!(await runAction(
			() => transitionHarvestProject(currentBackendBaseUrl(), projectId, target, lifecycleReason),
			`Project moved to ${target}.`
		))) return;
		lifecycleReason = '';
	}

	onMount(() => {
		if (manager.machines.size === 0) manager.connect(`${getBackendWsBase()}/ws`);
		operator = localStorage.getItem('harvest-operator') || 'Owner';
		const timer = window.setInterval(() => {
			if (project?.state !== 'active') return;
			const currentPage = pageGuard();
			void Promise.all([
				fetchHarvestProject(currentBackendBaseUrl(), projectId),
				fetchHarvestRuntime(currentBackendBaseUrl())
			]).then(([nextProject, nextRuntime]) => {
				if (!currentPage()) return;
				syncProject(nextProject, true);
				runtimeStatus = nextRuntime;
			}).catch(() => { /* Keep the last successful polling snapshot. */ });
		}, 3000);
		return () => window.clearInterval(timer);
	});

	$effect(() => {
		if (operator.trim()) localStorage.setItem('harvest-operator', operator.trim());
	});

	$effect(() => {
		// Re-run for either route or backend changes; cleanup invalidates outstanding work.
		pageIdentity;
		untrack(() => {
			pageGeneration++;
			resetProjectState();
			void loadProject();
		});
		return () => { pageGeneration++; };
	});

	$effect(() => {
		const runId = acceptanceEvidenceRunId();
		const expectedCount = expectedAcceptancePieceCount();
		if (!runId) {
			acceptancePieces = [];
			acceptancePieceImages = {};
			loadedAcceptanceRunId = null;
			loadedAcceptancePieceCount = -1;
			return;
		}
		if (runId === loadedAcceptanceRunId && expectedCount === loadedAcceptancePieceCount) return;
		void loadAcceptanceEvidence(runId, expectedCount);
	});
</script>

{#snippet bagProgressCards(value: HarvestProject)}
	<div class="mt-3 grid gap-3 lg:grid-cols-2">
		{#if value.progress.groups.length === 0}
			<div class="border border-border bg-bg p-4 text-sm text-text-muted">
				No part quotas are available for this project.
			</div>
		{:else}
			{#each value.progress.groups as group (group.group_id)}
				{@const percent = bagPercent(group)}
				{@const remaining = Math.max(0, group.required - group.confirmed)}
				{@const started = group.confirmed > 0}
				{@const parts = bagPartRows(value, group)}
				<div
					class={group.complete
						? 'overflow-hidden border border-success/50 bg-success/10'
						: started
							? 'overflow-hidden border border-info/40 bg-info/10'
							: 'overflow-hidden border border-border bg-bg'}
				>
					<button
						type="button"
						class="w-full p-4 text-left focus-visible:outline-2 focus-visible:outline-info"
						aria-expanded={Boolean(expandedBags[group.group_id])}
						aria-controls={`harvest-bag-${group.group_id}-parts`}
						onclick={() => toggleBag(group.group_id, parts)}
					>
						<div class="flex items-start justify-between gap-3">
							<div class="min-w-0">
								<div class="break-words font-semibold text-text" title={group.label}>
									{group.label}
								</div>
								<div class="mt-1 break-all font-mono text-xs text-info">
									Destination {bagDestination(value, group.group_id)}
								</div>
							</div>
							<div class="flex shrink-0 items-center gap-2 text-xs font-semibold">
								<span class={group.complete ? 'text-success' : started ? 'text-info' : 'text-text-muted'}>
									{group.complete ? 'Complete' : started ? 'In progress' : 'Not started'}
								</span>
								<ChevronDown
									class="h-4 w-4 transition-transform {expandedBags[group.group_id] ? 'rotate-180' : ''}"
								/>
							</div>
						</div>
						<div class="mt-3 flex flex-wrap items-end justify-between gap-2 text-sm">
							<div class="text-text">
								Project progress:
								<span class="font-semibold">{formatInteger(group.confirmed)} / {formatInteger(group.required)}</span>
								confirmed
							</div>
							<div class="text-text-muted">{formatInteger(remaining)} remaining · {percent}%</div>
						</div>
						<div
							class="mt-2 h-2 overflow-hidden bg-surface"
							role="progressbar"
							aria-label={`${group.label} progress`}
							aria-valuemin="0"
							aria-valuemax="100"
							aria-valuenow={percent}
						>
							<div
								class={group.complete ? 'h-full bg-success' : 'h-full bg-info'}
								style:width={`${percent}%`}
							></div>
						</div>
					</button>

					{#if expandedBags[group.group_id]}
						<div
							id={`harvest-bag-${group.group_id}-parts`}
							class="space-y-2 border-t border-border p-3"
						>
							{#if parts.length === 0}
								<div class="p-2 text-sm text-text-muted">No part rows are available for this bag.</div>
							{:else}
								{#each parts as part (`${group.group_id}:${part.part_id}:${part.color_id}`)}
									{@const displayPartName = partName(part.part_id, part.color_id)}
									{@const displayColorName = colorName(part.part_id, part.color_id)}
									<div class="grid grid-cols-[4rem_minmax(0,1fr)] gap-3 border border-border bg-surface p-2 sm:grid-cols-[4rem_minmax(0,1fr)_auto] sm:items-center">
										<div class="h-16 w-16 border border-border bg-bg p-1">
											<PieceThumb
												src={partImageUrl(part.part_id, part.color_id)}
												alt={displayPartName}
												fallbackText={part.part_id}
											/>
										</div>
										<div class="min-w-0">
											<div class="break-words font-medium text-text" title={displayPartName}>
												{displayPartName}
											</div>
											<div class="mt-1 break-all text-xs text-text-muted">
												Part {part.part_id} · {displayColorName}
											</div>
										</div>
										<div class="col-span-2 text-sm sm:col-span-1 sm:text-right">
											<div class="font-semibold text-text">{part.confirmed} / {part.required}</div>
											<div class={part.missing === 0 ? 'text-success' : 'text-text-muted'}>
												{part.missing === 0 ? 'Complete' : `${part.missing} remaining`}
											</div>
										</div>
									</div>
								{/each}
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		{/if}
	</div>
{/snippet}

<svelte:head
	><title>{project?.set_metadata?.name ?? project?.name ?? 'Harvest project'} · Sorter</title
	></svelte:head
>

<div class="min-h-screen bg-bg">
	<dialog bind:this={copyDialog} aria-labelledby="fresh-copy-title" aria-describedby="fresh-copy-description"
		class="m-auto w-full max-w-lg border border-border bg-surface p-5 text-text shadow-lg backdrop:bg-black/50">
		<h2 id="fresh-copy-title" class="text-lg font-semibold">Create fresh copy</h2>
		<div id="fresh-copy-description" class="my-4 space-y-2 text-sm text-text-muted">
			<p>A new project will reuse this project's saved frozen BOM, bags, and mappings and start at zero progress. This project and its history remain unchanged.</p>
			<p>Creating a copy does not stop the current project, empty bins, or start sorting. Complete the existing preparation and activation steps on the copy before sorting.</p>
			<p>Only saved setup is copied. Unsaved edits here are not saved or included, and will be discarded when you open the copy.</p>
		</div>
		<label for="fresh-copy-operator" class="text-sm">Operator</label>
		<Input id="fresh-copy-operator" bind:value={operator} disabled={Boolean(pendingCopy) || copying} />
		{#if pendingCopy}
			<p class="my-3 text-sm text-text-muted">Saved request: revision {pendingCopy.payload.expected_revision}, operator {pendingCopy.payload.operator}. Retries use this same request.</p>
		{/if}
		{#if !copyStorageReady && copyError}
			<Alert variant="danger">{copyError}</Alert>
		{:else if pendingCopy?.code === 'STALE_PROJECT_REVISION'}
			<Alert variant="danger">The saved setup changed. Refresh saved setup before deliberately submitting a new copy request.</Alert>
		{:else if pendingCopy?.code === 'COPY_REQUEST_CONFLICT'}
			<Alert variant="danger">Copy request conflict. This request ID is already associated with different copy details. The saved request has been retained; resolve the conflict before continuing.</Alert>
		{:else if copyError}
			<Alert variant="danger">{copyError} {pendingCopy?.copyId ? 'The created copy is retained below.' : pendingCopy ? 'The outcome may be uncertain. Retry the saved request to recover the same copy.' : 'No request was sent.'}</Alert>
		{/if}
		<div class="mt-5 flex flex-wrap gap-3">
			<Button variant="secondary" onclick={() => copyDialog.close()}>Close</Button>
			{#if pendingCopy?.copyId}
				<Button onclick={() => pendingCopy?.copyId && void openCopy(pendingCopy.copyId)}>Open created copy</Button>
				<Button variant="secondary" onclick={() => void prepareAnotherCopy()}>Prepare another copy</Button>
			{:else if pendingCopy?.code === 'STALE_PROJECT_REVISION'}
				<Button disabled={loading} onclick={() => void prepareAnotherCopy()}>Refresh saved setup</Button>
			{:else}
				<Button loading={copying} disabled={loading || busy || !copyStorageReady || Boolean(pendingCopy?.code) || (!pendingCopy && !operator.trim())} onclick={() => void submitFreshCopy()}>
					{pendingCopy ? 'Retry saved copy request' : 'Create copy from saved setup'}
				</Button>
			{/if}
		</div>
	</dialog>
	<AppHeader />

	<main class="mx-auto flex max-w-6xl flex-col gap-4 p-4 lg:p-6">
		<a
			href="/dashboard/harvest"
			class="inline-flex w-fit items-center gap-2 text-sm font-medium text-info hover:underline"
		>
			<ArrowLeft class="h-4 w-4" /> Back to Harvest portfolio
		</a>

		{#if error}<Alert variant="danger">{error}</Alert>{/if}
		{#if success}<Alert variant="success">{success}</Alert>{/if}

		{#if loading}
			<div class="border border-border bg-surface p-6 text-sm text-text-muted">
				Loading guided Harvest project…
			</div>
		{:else if project}
			<section class="border border-border bg-surface">
				<div class="flex flex-wrap items-start justify-between gap-4 border-b border-border p-4">
					<div class="flex items-start gap-4">
						{#if project.set_metadata?.image_url}
							<img
								src={project.set_metadata.image_url}
								alt={`${project.set_metadata.name} set`}
								class="h-24 w-24 border border-border bg-white object-contain p-1"
							/>
						{/if}
						<div>
							<div class="flex items-center gap-2">
								<ShieldCheck class="h-5 w-5 text-info" />
								<h1 class="text-xl font-bold text-text">
									{project.set_metadata?.name ?? project.name}
								</h1>
							</div>
							<div class="mt-1 text-sm text-text-muted">
								Set {project.set_number}{project.set_metadata
									? ` · ${project.set_metadata.year} · Official ${formatInteger(project.set_metadata.official_piece_count)} pieces`
									: ''}
							</div>
							<div class="mt-1 text-xs text-text-muted">
								Revision {project.revision} · {project.state} · {project.draft_source
									?.source_kind ?? 'saved draft'}
							</div>
							<div class="mt-1 text-xs text-text-muted">
								Confirmed {formatInteger(project.progress.summary.confirmed_quantity)} · Remaining {formatInteger(project.progress.summary.missing_quantity)}
							</div>
						</div>
					</div>
					<div class="text-right">
						<Button variant="secondary" size="sm" disabled={busy || copying} onclick={() => copyDialog.showModal()}>
							Create fresh copy
						</Button>
						<div
							class={project.state === 'active'
								? 'border border-success/40 bg-success/10 px-2 py-1 text-xs font-semibold tracking-wider text-success uppercase'
								: 'border border-warning/40 bg-warning/10 px-2 py-1 text-xs font-semibold tracking-wider text-warning uppercase'}
						>
							{project.state === 'active'
								? 'Live Harvest routing active'
								: project.state === 'completed'
									? 'Live Harvest run complete'
									: 'Guided activation workflow'}
						</div>
						<Button
							variant="secondary"
							size="sm"
							class="mt-2"
							{loading}
							onclick={() => void loadProject()}
						>
							<RefreshCw class="h-4 w-4" /> Refresh
						</Button>
					</div>
				</div>

				<div
					class="grid grid-cols-2 gap-1 border-b border-border p-3 sm:grid-cols-4 lg:grid-cols-8"
				>
					{#each stages as stage (stage.number)}
						<button
							type="button"
							class:!border-primary={openStep === stage.number}
							class="flex min-h-16 items-center gap-2 border border-border bg-bg px-2 py-2 text-left"
							onclick={() => (openStep = stage.number)}
						>
							{#if stageComplete(stage.number)}<CheckCircle2
									class="h-4 w-4 flex-none text-success"
								/>{:else}<Circle class="h-4 w-4 flex-none text-text-muted" />{/if}
							<span class="text-xs"
								><span class="block font-semibold text-text">{stage.number}. {stage.label}</span
								>{stageComplete(stage.number)
									? 'Complete'
									: openStep === stage.number
										? 'Current'
										: 'Pending'}</span
							>
						</button>
					{/each}
				</div>
			</section>

			<button
				type="button"
				class="sticky top-2 z-10 border border-info/40 bg-surface px-4 py-3 text-left shadow-sm"
				onclick={() => (openStep = nextAction().step)}
			>
				<div class="text-xs font-semibold tracking-wider text-info uppercase">Next action</div>
				<div class="mt-1 font-semibold text-text">{nextAction().title}</div>
				<div class="mt-1 text-sm text-text-muted">{nextAction().detail}</div>
			</button>

			{#if openStep === 1}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">1. Confirm the official inventory</h2>
					<p class="mt-1 text-sm text-text-muted">
						This immutable inventory proves what belongs in the complete set. It does not reserve
						bins or move hardware.
					</p>
					{#if project.bom}
						<div class="mt-3 border border-success/40 bg-success/10 p-3 text-sm">
							<div class="font-semibold text-text">
								<Check class="mr-1 inline h-4 w-4 text-success" /> Official inventory frozen
							</div>
							<div class="mt-1 text-text-muted">
								{project.bom.filename} · {formatInteger(project.bom.summary.total_quantity)} pieces ·
								{formatInteger(project.bom.summary.distinct_elements)} unique elements
							</div>
							{#if project.bom.runtime_aliases?.status === 'ready'}<div
									class="mt-1 text-text-muted"
								>
									Live recognition coverage: {formatInteger(
										project.bom.runtime_aliases.summary.covered_quantity
									)} / {formatInteger(project.bom.runtime_aliases.summary.sortable_quantity)} pieces
								</div>{:else}<div class="mt-2 font-medium text-warning">
									Refresh the official inventory to freeze the BrickLink identifiers required for live sorting.
								</div>{/if}
						</div>
					{:else}
						<div class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm text-text">
							No official inventory has been frozen yet.
						</div>
					{/if}
					<div class="mt-3 flex flex-wrap gap-2">
						<Button loading={busy} onclick={() => void fetchOfficialBom()}
							><Search class="h-4 w-4" />
							{project.bom ? 'Refresh official inventory' : 'Fetch official inventory'}</Button
						>
						<a
							href="/settings/api-keys"
							class="inline-flex items-center border border-border bg-bg px-3 py-2 text-sm font-medium text-text hover:bg-surface"
							>Rebrickable settings</a
						>
						{#if project.set_metadata?.set_url}<a
								href={project.set_metadata.set_url}
								target="_blank"
								rel="noopener noreferrer"
								class="inline-flex items-center gap-1 px-3 py-2 text-sm font-medium text-info underline"
								>View set <ExternalLink class="h-3 w-3" /></a
							>{/if}
					</div>
					<details class="mt-4 border-t border-border pt-3">
						<summary class="cursor-pointer text-sm font-medium text-text-muted"
							>Advanced: upload a private or exported inventory</summary
						>
						<div class="mt-3 flex flex-wrap gap-2">
							<select
								aria-label="Inventory file format"
								class="setup-control px-2 py-2 text-sm text-text"
								bind:value={bomProvider}
								><option value="private_moc">Private MOC / canonical</option><option
									value="rebrickable">Rebrickable export</option
								></select
							>
							<Button variant="secondary" loading={busy} onclick={() => bomInput?.click()}
								><FileUp class="h-4 w-4" /> Upload inventory</Button
							>
							<input
								bind:this={bomInput}
								type="file"
								accept=".json,.csv,application/json,text/csv"
								class="hidden"
								onchange={handleBomUpload}
							/>
						</div>
					</details>
				</section>
			{/if}

			{#if openStep === 2}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">2. Review only the differences</h2>
					<p class="mt-1 text-sm text-text-muted">
						The normal guided choice is exact matching, one destination per bag, and counted
						exclusion of non-sortable pieces.
					</p>
					{#if project.reconciliation.status === 'validated' || project.reconciliation.status === 'validated_with_extras'}
						<div class="mt-3 border border-success/40 bg-success/10 p-3 text-sm">
							<span class="font-semibold text-text"
								>All bag quantities match the frozen inventory.</span
							> No exception decisions are required.
						</div>
					{:else}
						<div class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm">
							<div class="font-semibold text-text">Exceptions need review</div>
							<div class="mt-1 text-text-muted">
								{project.reconciliation.unmapped.length} unmapped · {project.reconciliation.missing
									.length} missing · {project.reconciliation.overages.length} overages
							</div>
						</div>
					{/if}
					{#if project.reconciliation.issues.length > 0 || project.reconciliation.unmapped.length > 0}
						<div class="mt-3 divide-y divide-border border border-border">
							{#each project.reconciliation.issues as issue, index (`issue-${index}`)}<div
									class="p-2 text-sm"
								>
									<TriangleAlert class="mr-1 inline h-4 w-4 text-warning" />
									{String(issue.message ?? issue.code ?? 'Review required')}
								</div>{/each}
							{#each project.reconciliation.unmapped as item (`${item.group_id}-${item.part_id}-${item.color_id}`)}<div
									class="p-2 text-sm"
								>
									<span class="font-medium">{friendlyGroup(item.group_id)}</span> · part {item.part_id},
									color {item.color_id} · {formatInteger(item.quantity)} pieces
								</div>{/each}
						</div>
					{/if}
					<div class="mt-3 flex flex-wrap gap-2">
						<Button variant="secondary" onclick={applyRecommendedReview}
							>Use recommended settings</Button
						><Button loading={busy} disabled={!reviewDirty()} onclick={() => void saveReview()}
							>Save review decisions</Button
						>
					</div>
					{#if reviewDirty()}<div class="mt-2 text-xs font-medium text-warning">
							Unsaved review changes
						</div>{/if}
					<details class="mt-4 border-t border-border pt-3" bind:open={advancedOpen}>
						<summary class="cursor-pointer text-sm font-semibold text-text"
							>Advanced matching and bag destinations</summary
						>
						<div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
							<label class="text-xs text-text-muted"
								>Portfolio priority<Input
									type="number"
									min="0"
									max="10000"
									bind:value={priority}
								/></label
							>
							<label class="text-xs text-text-muted"
								>Part matching<select
									class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
									bind:value={matchPolicy}
									><option value="exact">Exact only</option><option value="compatible"
										>Compatible</option
									><option value="substitute">Substitutes allowed</option></select
								></label
							>
							<label class="text-xs text-text-muted"
								>Fallback behavior<select
									class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
									bind:value={fallbackMode}
									><option value="bag_plan">Keep bag plan</option><option value="adaptive_part"
										>Adaptive part groups</option
									><option value="inventory_first">Inventory first</option></select
								></label
							>
							<label class="text-xs text-text-muted"
								>Non-sortable pieces<select
									class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
									bind:value={nonSortableAction}
									><option value="exclude">Exclude with count</option><option value="include"
										>Include in destinations</option
									><option value="review">Require review</option></select
								></label
							>
						</div>
						<div class="mt-3 divide-y divide-border border border-border">
							{#each Object.entries(groupActions) as [groupId, action] (groupId)}<label
									class="flex items-center justify-between gap-3 p-2 text-sm"
									><span>{friendlyGroup(groupId)}</span><select
										class="setup-control px-2 py-1 text-sm text-text"
										value={action}
										onchange={(event) =>
											(groupActions = { ...groupActions, [groupId]: event.currentTarget.value })}
										><option value="separate">Separate destination</option><option value="include"
											>Combine</option
										><option value="exclude">Exclude</option><option value="review"
											>Needs review</option
										></select
									></label
								>{/each}
						</div>
						<label class="mt-3 block text-xs text-text-muted"
							>Namespace and substitution rules<textarea
								aria-label="Namespace and substitution rules"
								class="setup-control mt-1 min-h-40 w-full p-2 font-mono text-xs text-text"
								bind:value={mappingJson}
							></textarea></label
						>
					</details>
				</section>
			{/if}

			{#if openStep === 3}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">3. Build the assumed-empty bin plan</h2>
					<p class="mt-1 text-sm text-text-muted">
						Every enabled bin is treated as empty for planning. Existing contents and assignments
						remain untouched and become clearance warnings later.
					</p>
					{#if project.capacity_plan}
						<div class="mt-3 flex flex-wrap gap-2 text-sm">
							<span class="border border-border bg-bg px-2 py-1"
								>{project.capacity_plan.summary?.wave_count ?? 0} wave(s)</span
							><span class="border border-border bg-bg px-2 py-1"
								>{project.capacity_plan.summary?.required_groups ?? 0} destinations</span
							><span class="border border-border bg-bg px-2 py-1"
								>{project.capacity_plan.summary?.planned_bins_requiring_clearance ?? 0} currently non-clear</span
							>
						</div>
						{#each project.capacity_plan.waves as wave (wave.wave)}
							<div class="mt-3 border border-border bg-bg p-3">
								<div class="font-semibold text-text">Wave {wave.wave}</div>
								<div class="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
									{#each wave.assignments as item (`${wave.wave}-${item.group_id}`)}<div
											class="border border-border bg-surface p-2 text-sm"
										>
											<div class="font-medium text-text">{item.group_label}</div>
											<div class="text-text-muted">{formatInteger(item.quantity)} pieces</div>
											{#if editingAssignments}<select
													class="setup-control mt-1 w-full p-1 text-xs text-text"
													value={capacityAssignments[item.group_id] ?? item.bin_id}
													onchange={(event) =>
														(capacityAssignments = {
															...capacityAssignments,
															[item.group_id]: event.currentTarget.value
														})}
													>{#each project.capacity_plan?.bins.filter((bin) => bin.available) ?? [] as bin (bin.bin_id)}<option
															value={bin.bin_id}>{bin.bin_id}</option
														>{/each}</select
												>{:else}<div class="mt-1 font-mono text-xs text-info">
													{item.bin_id}
												</div>{/if}
										</div>{/each}
								</div>
							</div>
						{/each}
					{:else}<div class="mt-3 border border-border bg-bg p-3 text-sm text-text-muted">
							No bin plan has been generated for this revision.
						</div>{/if}
					<div class="mt-3 flex flex-wrap gap-2">
						<Button
							loading={busy}
							disabled={!project.readiness.gates.bom_reconciled ||
								!project.readiness.gates.review_policies_resolved}
							onclick={() => void planCapacity(false)}
							><Layers3 class="h-4 w-4" /> Build recommended plan</Button
						>{#if project.capacity_plan}{#if editingAssignments}<Button
									variant="secondary"
									loading={busy}
									onclick={() => void planCapacity(true)}>Save custom assignments</Button
								><Button variant="ghost" onclick={() => (editingAssignments = false)}>Cancel</Button
								>{:else}<Button variant="secondary" onclick={() => (editingAssignments = true)}
									>Customize assignments</Button
								>{/if}{/if}
					</div>
				</section>
			{/if}

			{#if openStep === 4}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">4. Run the complete simulation</h2>
					<p class="mt-1 text-sm text-text-muted">
						This checks every required quantity against the frozen inventory and planned build waves
						without moving hardware.
					</p>
					{#if project.simulation}<div class="mt-3 grid gap-2 sm:grid-cols-3">
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Status</div>
								<div class="font-semibold text-text">{project.simulation.status}</div>
							</div>
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Routed</div>
								<div class="font-semibold text-text">
									{formatInteger(project.simulation.summary.routed_quantity)}
								</div>
							</div>
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Missing</div>
								<div class="font-semibold text-text">
									{formatInteger(project.simulation.summary.missing_quantity)}
								</div>
							</div>
						</div>{/if}
					<div class="mt-3 flex gap-2">
						<Button
							loading={busy}
							disabled={!project.readiness.gates.capacity_plan_ready}
							onclick={() => void runSimulation()}
							><Play class="h-4 w-4" /> Run complete simulation</Button
						><a
							href={missingPartsCsvUrl(currentBackendBaseUrl(), projectId)}
							download
							class="inline-flex items-center gap-2 border border-border bg-bg px-3 py-2 text-sm font-medium text-text"
							><Download class="h-4 w-4" /> Missing-parts CSV</a
						>
					</div>
				</section>
			{/if}

			{#if openStep === 5}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">5. Run a controlled physical test</h2>
					<p class="mt-1 text-sm text-text-muted">
						This temporarily installs the simulated bag destinations for a limited test. It never
						starts motion by itself, automatically pauses at the configured piece limit, and records
						the actual confirmed drops as acceptance evidence.
					</p>
					{#if project.acceptance}<div class="mt-3 border border-border bg-bg p-3 text-sm">
							<div class="font-semibold text-text">
								Latest result: {String(project.acceptance.result)}
							</div>
							<div class="mt-1 text-text-muted">
								Operator {String(project.acceptance.operator)} · {formatInteger(
									Number(project.acceptance.controlled_piece_count)
								)} controlled pieces
							</div>
						</div>{/if}
					{#if !activeAcceptance() && acceptanceEvidenceRunId()}
						{#if acceptancePiecesError}<Alert variant="danger">{acceptancePiecesError}</Alert>{/if}
						<AcceptancePieceEvidence
							items={acceptancePieces}
							imageStates={acceptancePieceImages}
							loading={acceptancePiecesLoading}
						/>
					{/if}
					{#if activeAcceptance()}{@const acceptance = activeAcceptance()}
						{#if acceptance}
							<div
								class={acceptance.status === 'awaiting_observation'
									? 'mt-3 border border-success/40 bg-success/10 p-4'
									: 'mt-3 border border-info/40 bg-info/10 p-4'}
							>
								<div class="font-semibold text-text">
									{acceptanceReplacementPieceCount() > 0
										? 'Corrected evidence needs replacement pieces'
										: acceptance.status === 'awaiting_observation'
										? 'Routing limit reached — inspect the bins'
										: runtimeStatus?.sorter_state === 'paused'
											? 'Controlled test armed — click Resume in the global header'
											: 'Controlled test is running'}
								</div>
								<div class="mt-1 text-sm text-text-muted">
									Confirmed {formatInteger(acceptance.runtime_progress?.confirmed_piece_count ?? 0)} of
									{formatInteger(acceptance.test_piece_limit ?? acceptancePieceCount)} test pieces. Sorter:
									{runtimeStatus?.sorter_state ??
										(runtimeStatus?.acceptance_evidence_recording_allowed ? 'standby' : 'unknown')}.
								</div>
							</div>
							{#if acceptanceReplacementPieceCount() > 0}
								<div class="mt-3 border border-warning/50 bg-warning/10 p-4 text-sm text-text">
									<div class="font-semibold">The test evidence was corrected after auto-pause.</div>
									<p class="mt-1 text-text-muted">
										{acceptanceReplacementPieceCount()} of {acceptance.test_piece_limit} test positions now need replacement pieces. Reopening preserves the confirmed evidence and does not start motion.
									</p>
									<Button
										class="mt-3"
										loading={busy}
										disabled={runtimeStatus?.sorter_state !== 'paused' || !operator.trim()}
										onclick={() => void reopenAcceptanceAfterCorrection()}
									>
										Continue with {acceptanceReplacementPieceCount()} replacement piece{acceptanceReplacementPieceCount() === 1 ? '' : 's'}
									</Button>
								</div>
							{/if}

							<div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
								{#each acceptance.assignments as assignment (assignment.group_id)}<div
									class="border border-border bg-bg p-3 text-sm"
								>
									<div class="font-semibold text-text">{assignment.group_label}</div>
									<div class="mt-1 font-mono text-xs text-info">{assignment.bin_id}</div>
								</div>{/each}
							</div>
							{#if acceptancePiecesError}<Alert variant="danger">{acceptancePiecesError}</Alert>{/if}
							<AcceptancePieceEvidence
								items={acceptancePieces}
								imageStates={acceptancePieceImages}
								loading={acceptancePiecesLoading}
							/>

							{#if acceptance.status === 'awaiting_observation'}
								<div class="mt-4 border-t border-border pt-4">
									<h3 class="font-semibold text-text">What the sorter recorded</h3>
									{#if runtimeStatus?.acceptance_evidence_recording_allowed && runtimeStatus.sorter_state !== 'paused'}
										<div class="mt-2 border border-info/40 bg-info/10 p-3 text-sm text-text">
											The sorter is safely stopped in hardware standby. You can record this
											inspection without restarting or moving the machine.
										</div>
									{/if}
									<div class="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
										{#each acceptance.observed_routes ?? [] as route (route.group_id)}<div
											class="border border-border bg-bg p-3 text-sm"
										>
											<div class="font-semibold text-text">{route.group_label}</div>
											<div class="mt-1 text-text-muted">
												{route.piece_count} piece{route.piece_count === 1 ? '' : 's'} →
												<span class="font-mono text-info">{route.bin_id ?? 'unknown bin'}</span>
											</div>
										</div>{/each}
									</div>
									<label class="mt-3 flex items-start gap-2 text-sm text-text"
										><input type="checkbox" class="mt-1" bind:checked={acceptanceObservedMatch} /> I
										physically inspected the bins and every test piece landed in the displayed destination.</label
									>
									<label class="mt-3 block text-xs text-text-muted"
										>Observation notes<textarea
											class="setup-control mt-1 min-h-20 w-full p-2 text-sm text-text"
											bind:value={acceptanceNotes}
										></textarea></label
									>
									<div class="mt-3 flex flex-wrap gap-2">
										<Button
											loading={busy}
											disabled={!runtimeStatus?.acceptance_evidence_recording_allowed ||
												!acceptanceObservedMatch ||
												!operator.trim()}
											onclick={() => void finishAcceptance('passed')}>Record inspected pass</Button
										><Button
											variant="secondary"
											loading={busy}
											disabled={!runtimeStatus?.acceptance_evidence_recording_allowed || !operator.trim()}
											onclick={() => void finishAcceptance('failed')}>Record inspected failure</Button
										>
									</div>
								</div>
							{:else}
								<div class="mt-4 border-t border-border pt-3">
									<label class="block text-xs text-text-muted"
										>Stop reason<Input bind:value={acceptanceAbortReason} /></label
									>
									<Button
										class="mt-2"
										variant="secondary"
										loading={busy}
										disabled={runtimeStatus?.sorter_state !== 'paused' ||
											!acceptanceAbortReason.trim() ||
											!operator.trim()}
										onclick={() => void abortAcceptance()}>Stop controlled test</Button
									>
									{#if runtimeStatus?.sorter_state !== 'paused'}<div class="mt-2 text-xs text-warning">
										Pause the sorter from the global header before stopping the test.
									</div>{/if}
								</div>
							{/if}
						{/if}
					{:else}
						<div class="mt-3 border border-border bg-bg p-4 text-sm text-text">
							<div class="font-semibold">Before you arm the test</div>
							<ol class="mt-2 list-decimal space-y-1 pl-5 text-text-muted">
								<li>Pause the sorter.</li>
								<li>Physically empty every displayed test bin below.</li>
								<li>Arm the test here, then click Resume in the global header.</li>
								<li>Feed only the configured number of pieces. The sorter pauses automatically.</li>
								<li>Inspect the bins and record pass or failure from the captured results.</li>
							</ol>
						</div>
						<div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
							{#each (project.capacity_plan?.waves ?? []).flatMap((wave) => wave.assignments) as assignment (assignment.group_id)}<div
								class="border border-border bg-bg p-3 text-sm"
							>
								<div class="font-semibold text-text">{assignment.group_label}</div>
								<div class="mt-1 font-mono text-xs text-info">{assignment.bin_id}</div>
							</div>{/each}
						</div>
						<div class="mt-3 grid gap-3 sm:grid-cols-3">
							<label class="text-xs text-text-muted"
								>Test identifier<Input bind:value={acceptanceTestId} /></label
							><label class="text-xs text-text-muted">Operator<Input bind:value={operator} /></label
							><label class="text-xs text-text-muted"
								>Piece limit (1–25)<Input
									type="number"
									min="1"
									max="25"
									bind:value={acceptancePieceCount}
								/></label
							>
						</div>
						<label class="mt-3 flex items-start gap-2 text-sm text-text"
							><input type="checkbox" class="mt-1" bind:checked={acceptanceBinsEmpty} /> I physically
							emptied every displayed test bin and verified that no pieces remain.</label
						>
						{#if runtimeStatus?.sorter_state !== 'paused'}<div
								class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm text-text"
							>
								Pause the sorter from the global header before arming the test.
							</div>{/if}
						<div class="mt-3">
							<Button
								loading={busy}
								disabled={!project.readiness.ready_for_physical_acceptance ||
									runtimeStatus?.sorter_state !== 'paused' ||
									Boolean(runtimeStatus?.active) ||
									!binReadiness ||
									binReadiness.status !== 'clear' ||
									!acceptanceBinsEmpty ||
									!acceptanceTestId.trim() ||
									!operator.trim() ||
									acceptancePieceCount < 1 ||
									acceptancePieceCount > 25}
								onclick={() => void armAcceptance()}><ShieldCheck class="h-4 w-4" /> Arm controlled test</Button
							>
						</div>
					{/if}
				</section>
			{/if}

			{#if openStep === 6}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">6. Verify and clear the planned bins</h2>
					<p class="mt-1 text-sm text-text-muted">
						This checks recorded assignments and counts. Pause the sorter and physically empty each
						selected bin before clearing its records and releasing its previous assignment.
					</p>
					<div class="mt-3">
						<Button variant="secondary" loading={busy} onclick={() => void checkBins()}
							><RefreshCw class="h-4 w-4" /> Check recorded bin state</Button
						>
					</div>
					{#if binReadiness}
						{#if binReadiness.status === 'clear'}<div
								class="mt-3 border border-success/40 bg-success/10 p-3 text-sm"
							>
								<span class="font-semibold text-text"
									>All {binReadiness.suggested_bin_count} planned bins are clear in recorded state.</span
								> Perform the final physical verification before green-lighting.
							</div>{:else}
							<div class="mt-3 flex flex-wrap gap-2">
								<Button
									size="sm"
									variant={clearanceScope === 'suggested' ? 'primary' : 'secondary'}
									onclick={() => (clearanceScope = 'suggested')}
									>Suggested bins ({binReadiness.suggested_bins_requiring_clearance.length})</Button
								><Button
									size="sm"
									variant={clearanceScope === 'all' ? 'primary' : 'secondary'}
									onclick={() => (clearanceScope = 'all')}
									>All non-clear bins ({binReadiness.all_bins_requiring_clearance.length})</Button
								>
							</div>
							<div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
								{#each selectedClearanceBins() as bin (bin.bin_id)}<div
										class="border border-warning/40 bg-warning/10 p-3 text-sm"
									>
										<div class="font-mono font-semibold text-text">{bin.bin_id}</div>
										<div class="mt-1 text-text-muted">
											{bin.tracked_piece_count} recorded pieces · {bin.reserved_by.length} assignment(s)
										</div>
									</div>{/each}
							</div>
							{#if !binReadiness.record_clearance_allowed}<div
									class="mt-3 border border-danger/40 bg-danger/10 p-3 text-sm text-text"
								>
									<span class="font-semibold">Pause the sorter first.</span> The backend will reject record
									clearing while the sorter is running.
								</div>{/if}
							<label class="mt-3 flex items-start gap-2 text-sm text-text"
								><input type="checkbox" class="mt-1" bind:checked={physicalBinsEmptied} /> I physically
								emptied every selected bin and understand that its previous assignment will be released.</label
							>
							<div class="mt-3">
								<Button
									loading={busy}
									disabled={!physicalBinsEmptied ||
										!operator.trim() ||
										!binReadiness.record_clearance_allowed}
									onclick={() => void clearSelectedBins()}
									>Mark selected bins physically emptied</Button
								>
							</div>
						{/if}
					{/if}
				</section>
			{/if}

			{#if openStep === 7}
				<section class="border border-border bg-surface p-4">
					<h2 class="text-lg font-semibold text-text">7. Operator green light</h2>
					{#if project.readiness.green_light_approved && project.green_light}
						<div class="mt-3 border border-success/40 bg-success/10 p-4">
							<div class="flex items-center gap-2 text-lg font-semibold text-text">
								<CheckCircle2 class="h-5 w-5 text-success" /> Deployment green light recorded
							</div>
							<div class="mt-2 text-sm text-text-muted">
								Approved by {project.green_light.operator} on {formatDate(
									project.green_light.recorded_at
								)}.
							</div>
							<div class="mt-1 text-sm text-text">{project.green_light.reason}</div>
							<div class="mt-3 text-xs font-medium text-info">
								This approval is audited. Continue to step 8 while the sorter is paused to install the
								exact live bag assignments; activation itself does not start motion.
							</div>
						</div>
					{:else}
						<p class="mt-1 text-sm text-text-muted">
							The green light binds this project revision, simulation, acceptance record, and
							current bin state. Any relevant change invalidates it.
						</p>
						{#if !project.readiness.ready_for_green_light}<div
								class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm text-text"
							>
								Complete the simulation and controlled acceptance first.
							</div>{/if}
						{#if !binReadiness || binReadiness.status !== 'clear'}<div
								class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm text-text"
							>
								Recheck the planned bins and make sure their recorded state is clear.
							</div>{/if}
						<div class="mt-3 grid gap-3 sm:grid-cols-2">
							<label class="text-xs text-text-muted">Operator<Input bind:value={operator} /></label
							><label class="text-xs text-text-muted"
								>Approval reason<Input bind:value={greenLightReason} /></label
							>
						</div>
						<label class="mt-3 flex items-start gap-2 text-sm text-text"
							><input type="checkbox" class="mt-1" bind:checked={greenLightConfirmed} /> I physically
							verified every planned bin is empty and approve this exact project revision for the deployment
							release.</label
						>
						<div class="mt-3">
							<Button
								loading={busy}
								disabled={!project.readiness.ready_for_green_light ||
									!binReadiness ||
									binReadiness.status !== 'clear' ||
									!binReadiness.record_clearance_allowed ||
									!greenLightConfirmed ||
									!greenLightReason.trim() ||
									!operator.trim()}
								onclick={() => void greenLight()}
								><ShieldCheck class="h-4 w-4" /> Record deployment green light</Button
							>
						</div>
					{/if}
				</section>
			{/if}

			{#if openStep === 8}
				<section class="border border-border bg-surface p-4">
					<h2 class="flex items-center gap-2 text-lg font-semibold text-text">
						<Power class="h-5 w-5" /> 8. Live bag sorting
					</h2>
					{#if project.state === 'active' &&
					project.activation?.status === 'active' &&
					(project.activation.runtime_mode ?? 'live') === 'live'}
						<div class="mt-3 border border-success/40 bg-success/10 p-4">
							<div class="text-lg font-semibold text-text">Live Harvest routing is active</div>
							<div class="mt-1 text-sm text-text-muted">
								The sorter is {runtimeStatus?.sorter_state ?? 'unknown'}. {runtimeStatus?.sorter_state ===
								'paused'
									? 'Use the global header control to resume when the set pieces are ready.'
									: 'Each completed physical drop is being confirmed against the official bag quotas.'}
							</div>
						</div>
						<div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Bag-routed this run</div>
								<div class="text-xl font-semibold text-text">
									{formatInteger(liveRunBagCount())}
								</div>
							</div>
							<div class="border border-warning/40 bg-warning/10 p-3">
								<div class="text-xs text-text-muted">Exceptions this run</div>
								<div class="text-xl font-semibold text-text">
									{formatInteger(liveRunExceptionCount())}
								</div>
							</div>
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Live exception rate</div>
								<div class="text-xl font-semibold text-text">{liveRunExceptionRate().toFixed(1)}%</div>
							</div>
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Still needed</div>
								<div class="text-xl font-semibold text-text">
									{formatInteger(project.progress.summary.missing_quantity)}
								</div>
							</div>
							<div class="border border-border bg-bg p-3">
								<div class="text-xs text-text-muted">Bags complete</div>
								<div class="text-xl font-semibold text-text">
									{project.progress.summary.complete_groups} / {project.progress.summary.group_count}
								</div>
							</div>
						</div>
						{#if Number(project.allocation_summary?.exception_confirmed ?? 0) !== liveRunExceptionCount()}<div
								class="mt-2 text-xs text-text-muted"
							>
								Cumulative audit history: {formatInteger(
									project.allocation_summary?.exception_confirmed ?? 0
								)} exceptions, including controlled tests and prior runs.
							</div>{/if}
						{@render bagProgressCards(project)}
						{#each project.activation.assignments.filter((assignment) => assignment.group_id === 'harvest-exception') as assignment (assignment.group_id)}
							<div class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm">
								<div class="font-semibold text-text">{assignment.group_label}</div>
								<div class="mt-1 font-mono text-xs text-info">{assignment.bin_id}</div>
							</div>
						{/each}
						<div class="mt-4 border-t border-border pt-3">
							<label class="block text-xs text-text-muted"
								>Stop reason<Input bind:value={deactivationReason} /></label
							>
							<Button
								class="mt-2"
								variant="secondary"
								loading={busy}
								disabled={runtimeStatus?.sorter_state !== 'paused' || !deactivationReason.trim()}
								onclick={() => void stopLiveSorting()}>Stop live Harvest routing</Button
							>
							{#if runtimeStatus?.sorter_state !== 'paused'}<div class="mt-2 text-xs text-warning">
									Pause the sorter from the global header before stopping the live project.
								</div>{/if}
						</div>
					{:else if project.state === 'completed'}
						<div class="mt-3 border border-success/40 bg-success/10 p-4">
							<div class="flex items-center gap-2 text-lg font-semibold text-text">
								<CheckCircle2 class="h-5 w-5 text-success" /> Official quantities complete
							</div>
							<div class="mt-2 text-sm text-text-muted">
								{formatInteger(project.progress.summary.confirmed_quantity)} pieces were confirmed into
								{project.progress.summary.group_count} destinations. The completion gate paused the sorter.
							</div>
						</div>
						{@render bagProgressCards(project)}
					{:else}
						<p class="mt-1 text-sm text-text-muted">
							Activation installs the exact bag and exception-bin assignments from the green-lit plan.
							It does not start the feeder or move any hardware.
						</p>
						{#if runtimeStatus?.active && runtimeStatus.active.project_id !== project.project_id}<div
								class="mt-3 border border-danger/40 bg-danger/10 p-3 text-sm text-text"
							>
								Another Harvest project is active: {runtimeStatus.active.name}.
							</div>{/if}
						{#if runtimeStatus?.sorter_state !== 'paused'}<div
								class="mt-3 border border-warning/40 bg-warning/10 p-3 text-sm text-text"
							>
								Pause the sorter from the global header before activation.
							</div>{/if}
						<div class="mt-3 grid gap-3 sm:grid-cols-2">
							<label class="text-xs text-text-muted">Operator<Input bind:value={operator} /></label>
							<label class="text-xs text-text-muted"
								>Activation reason<Input bind:value={activationReason} /></label
							>
						</div>
						<label class="mt-3 flex items-start gap-2 text-sm text-text"
							><input type="checkbox" class="mt-1" bind:checked={activationConfirmed} /> I am
							activating this exact green-lit revision and understand that resuming the sorter will send
							physical pieces to the displayed bag and exception bins.</label
						>
						<div class="mt-3">
							<Button
								loading={busy}
								disabled={!project.readiness.activation_eligible ||
									runtimeStatus?.sorter_state !== 'paused' ||
									Boolean(runtimeStatus?.active) ||
									!binReadiness ||
									!activationConfirmed ||
									!operator.trim() ||
									!activationReason.trim()}
								onclick={() => void activateLiveSorting()}
								><Power class="h-4 w-4" /> Activate live bag sorting</Button
							>
						</div>
					{/if}
				</section>
			{/if}

			<details class="border border-border bg-surface" bind:open={advancedOpen}>
				<summary
					class="flex cursor-pointer items-center justify-between gap-2 p-4 font-semibold text-text"
					>Advanced validation, lifecycle, and audit <ChevronDown class="h-4 w-4" /></summary
				>
				<div class="grid gap-4 border-t border-border p-4 lg:grid-cols-2">
					<div class="border border-border bg-bg p-3">
						<div class="flex items-center gap-2 font-semibold text-text">
							<Route class="h-4 w-4" /> Simulated piece ledger
						</div>
						<p class="mt-1 text-xs text-text-muted">
							Optional diagnostic tool. Normal guided approval does not require {formatInteger(
								project.progress.summary.required_quantity
							)} manual entries.
						</p>
						<div class="mt-2 grid gap-2 sm:grid-cols-3">
							<Input placeholder="Piece id" bind:value={simulatedPieceId} /><Input
								placeholder="Part id"
								bind:value={simulatedPartId}
							/><Input placeholder="Color id" bind:value={simulatedColorId} />
						</div>
						<div class="mt-2 flex items-center justify-between gap-2 text-sm">
							<span
								>{formatInteger(project.progress.summary.confirmed_quantity)} / {formatInteger(
									project.progress.summary.required_quantity
								)} diagnostics confirmed</span
							><Button
								size="sm"
								variant="secondary"
								loading={busy}
								disabled={!project.readiness.gates.simulation_passed ||
									!simulatedPieceId ||
									!simulatedPartId ||
									!simulatedColorId}
								onclick={() => void simulateLedgerPiece()}>Confirm simulated piece</Button
							>
						</div>
						{#if (project.allocations ?? []).length > 0}<label
								class="mt-3 block text-xs text-text-muted"
								>Undo reason<Input bind:value={undoReason} /></label
							>
							<div class="mt-2 max-h-40 divide-y divide-border overflow-auto border border-border">
								{#each (project.allocations ?? [])
									.slice()
									.reverse()
									.slice(0, 8) as allocation (allocation.allocation_id)}<div
										class="flex items-center justify-between gap-2 p-2 text-xs"
									>
										<span>{allocation.piece_id} → {allocation.group_id} · {allocation.status}</span
										>{#if allocation.status !== 'undone'}<Button
												size="sm"
												variant="ghost"
												disabled={!undoReason.trim()}
												onclick={() => void undoAllocation(allocation.allocation_id)}>Undo</Button
											>{/if}
									</div>{/each}
							</div>{/if}
					</div>
					<div class="border border-border bg-bg p-3">
						<div class="font-semibold text-text">Project lifecycle</div>
						<div class="mt-1 text-xs text-text-muted">
							Current state: {project.state}. Manual changes require a reason.
						</div>
						<label class="mt-2 block text-xs text-text-muted"
							>Reason<Input bind:value={lifecycleReason} /></label
						>
						<div class="mt-2 flex gap-2">
							{#if project.state === 'paused' || project.state === 'completed'}<Button
									size="sm"
									variant="secondary"
									disabled={!lifecycleReason.trim()}
									onclick={() => void transition('review')}>Reopen in review</Button
								>{:else}<Button
									size="sm"
									variant="secondary"
									disabled={!lifecycleReason.trim()}
									onclick={() => void transition('paused')}>Pause project</Button
								>{/if}{#if project.state === 'accepted' || project.state === 'approved'}<Button
									size="sm"
									disabled={!lifecycleReason.trim() ||
										project.progress.summary.missing_quantity > 0}
									onclick={() => void transition('completed')}>Complete</Button
								>{/if}
						</div>
					</div>
				</div>
				<div class="border-t border-border p-4">
					<div class="font-semibold text-text">Tamper-evident audit history</div>
					<div class="mt-2 max-h-72 divide-y divide-border overflow-auto border border-border">
						{#each (project.events ?? []).slice().reverse() as event (event.event_id)}<div
								class="p-2 text-xs"
							>
								<div class="flex items-center justify-between gap-2">
									<span class="font-semibold text-text"
										>#{event.sequence} {eventLabel(event.kind)}</span
									><span class="text-text-muted">{formatDate(event.created_at)}</span>
								</div>
								<details class="mt-1">
									<summary class="cursor-pointer text-text-muted">Technical evidence</summary>
									<div class="mt-1 font-mono break-all text-text-muted">{event.event_hash}</div>
								</details>
							</div>{/each}
					</div>
				</div>
			</details>
		{/if}
	</main>
</div>
