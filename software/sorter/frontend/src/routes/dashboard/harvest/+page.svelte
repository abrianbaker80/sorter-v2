<script lang="ts">
	import { onMount } from 'svelte';
	import { getMachinesContext } from '$lib/machines/context';
	import { getBackendHttpBase, getBackendWsBase, machineHttpBaseUrlFromWsUrl } from '$lib/backend';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import MachineDropdown from '$lib/components/MachineDropdown.svelte';
	import { Alert, Button, Input } from '$lib/components/primitives';
	import {
		bricksPerBagSetUrl,
		confirmHarvestAllocation,
		createHarvestProject,
		createLocabriquesDraft,
		fetchHarvestBinReadiness,
		fetchHarvestBomFromRebrickable,
		fetchHarvestDraft,
		fetchHarvestDraftSummaries,
		fetchHarvestProject,
		fetchHarvestProjectSummaries,
		lookupLocabriques,
		missingPartsCsvUrl,
		planHarvestCapacity,
		proposeHarvestAllocation,
		simulateHarvestProject,
		transitionHarvestProject,
		undoHarvestAllocation,
		updateHarvestReview,
		uploadHarvestBom,
		uploadHarvestBsxDraft,
		type HarvestDraft,
		type HarvestBinReadiness,
		type HarvestProject,
		type HarvestProjectSummary,
		type HarvestSourceLookup,
		type HarvestValidationIssue
	} from '$lib/harvest/api';
	import {
		ClipboardList,
		Download,
		ExternalLink,
		FileCheck2,
		FileUp,
		Layers3,
		Play,
		Route,
		Search,
		ShieldCheck
	} from 'lucide-svelte';

	const manager = getMachinesContext();
	let activeBaseUrl = $state(currentBackendBaseUrl());

	let setNumber = $state('');
	let lookupResult = $state<HarvestSourceLookup | null>(null);
	let lookupLoading = $state(false);
	let locabriquesImporting = $state(false);
	let lookupError = $state<string | null>(null);
	let uploading = $state(false);
	let uploadError = $state<string | null>(null);
	let uploadSuccess = $state<string | null>(null);
	let fileInput = $state<HTMLInputElement | null>(null);
	let drafts = $state<HarvestDraft[]>([]);
	let draftsLoading = $state(true);
	let draftsError = $state<string | null>(null);
	let previewingDraftId = $state<string | null>(null);
	let activeDraft = $state<HarvestDraft | null>(null);
	let projects = $state<HarvestProject[]>([]);
	let projectSummaries = $state<HarvestProjectSummary[]>([]);
	let projectsLoading = $state(true);
	let projectBusy = $state(false);
	let projectError = $state<string | null>(null);
	let projectSuccess = $state<string | null>(null);
	let activeProject = $state<HarvestProject | null>(null);
	let bomInput = $state<HTMLInputElement | null>(null);
	let bomProvider = $state('private_moc');
	let groupActions = $state<Record<string, string>>({});
	let mappingJson = $state('');
	let projectPriority = $state(100);
	let projectMatchPolicy = $state('exact');
	let projectFallbackMode = $state('bag_plan');
	let nonSortableAction = $state('exclude');
	let simulatedPieceId = $state('');
	let simulatedPartId = $state('');
	let simulatedColorId = $state('');
	let undoReason = $state('Correction during draft validation');
	let lifecycleReason = $state('');
	let capacityAssignments = $state<Record<string, string>>({});
	let editingCapacityAssignments = $state(false);
	let binReadiness = $state<HarvestBinReadiness | null>(null);
	let clearanceScope = $state<'suggested' | 'all'>('suggested');

	function currentBackendBaseUrl(): string {
		return (
			machineHttpBaseUrlFromWsUrl(
				manager.selectedMachine?.status === 'connected' ? manager.selectedMachine.url : null
			) ?? getBackendHttpBase()
		);
	}

	function resetLookupForInput() {
		lookupResult = null;
		lookupError = null;
		uploadError = null;
		uploadSuccess = null;
		activeDraft = null;
	}

	function validateSetNumber(value: string): string | null {
		if (!/^[0-9]{3,7}(?:-[0-9]+)?$/.test(value)) {
			return 'Enter 3-7 digits with an optional variant suffix, such as 21369 or 21369-1.';
		}
		return null;
	}

	async function runLookup() {
		const normalized = setNumber.trim();
		const validationError = validateSetNumber(normalized);
		if (validationError) {
			lookupError = validationError;
			lookupResult = null;
			return;
		}

		lookupLoading = true;
		lookupError = null;
		uploadError = null;
		uploadSuccess = null;
		activeDraft = null;
		try {
			lookupResult = await lookupLocabriques(currentBackendBaseUrl(), normalized);
			setNumber = lookupResult.set_number;
		} catch (e: unknown) {
			lookupResult = null;
			lookupError = e instanceof Error ? e.message : 'LocaBriques lookup failed';
		} finally {
			lookupLoading = false;
		}
	}

	function submitLookup(event: SubmitEvent) {
		event.preventDefault();
		void runLookup();
	}

	async function loadDrafts(silent = false) {
		if (!silent) draftsLoading = true;
		try {
			drafts = await fetchHarvestDraftSummaries(currentBackendBaseUrl());
			draftsError = null;
		} catch (e: unknown) {
			if (!silent) {
				draftsError = e instanceof Error ? e.message : 'Failed to load Harvest drafts';
			}
		} finally {
			if (!silent) draftsLoading = false;
		}
	}

	function syncProjectEditor(project: HarvestProject) {
		groupActions = { ...project.policy.group_actions };
		mappingJson = JSON.stringify(project.mappings, null, 2);
		projectPriority = project.priority;
		projectMatchPolicy = project.match_policy;
		projectFallbackMode = project.fallback_mode;
		nonSortableAction = project.policy.non_sortable_action;
		capacityAssignments = Object.fromEntries(
			(project.capacity_plan?.waves ?? []).flatMap((wave) =>
				wave.assignments.map((assignment) => [assignment.group_id, assignment.bin_id])
			)
		);
		editingCapacityAssignments = false;
		binReadiness = null;
	}

	function updateProjectState(project: HarvestProject) {
		activeProject = project;
		projects = projects.some((item) => item.project_id === project.project_id)
			? projects.map((item) => (item.project_id === project.project_id ? project : item))
			: [...projects, project];
		projects = [...projects].sort(
			(a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at)
		);
		syncProjectEditor(project);
	}

	async function loadProjects(silent = false) {
		if (!silent) projectsLoading = true;
		try {
			projectSummaries = await fetchHarvestProjectSummaries(currentBackendBaseUrl());
			projectError = null;
		} catch (e: unknown) {
			if (!silent)
				projectError = e instanceof Error ? e.message : 'Failed to load Harvest projects';
		} finally {
			if (!silent) projectsLoading = false;
		}
	}

	async function selectProject(projectId: string) {
		projectBusy = true;
		projectError = null;
		try {
			updateProjectState(await fetchHarvestProject(currentBackendBaseUrl(), projectId));
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to load Harvest project';
		} finally {
			projectBusy = false;
		}
	}

	async function createProjectFromDraft() {
		if (!activeDraft) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			const project = await createHarvestProject(currentBackendBaseUrl(), activeDraft.draft_id);
			window.location.assign(
				`/dashboard/harvest/projects/${encodeURIComponent(project.project_id)}`
			);
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to create Harvest project';
		} finally {
			projectBusy = false;
		}
	}

	async function handleBomUpload(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file || !activeProject) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			const project = await uploadHarvestBom(
				currentBackendBaseUrl(),
				activeProject.project_id,
				file,
				bomProvider
			);
			updateProjectState(project);
			projectSuccess = `Frozen BOM revision ${project.bom?.bom_revision_id}.`;
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to freeze the BOM';
		} finally {
			projectBusy = false;
		}
	}

	async function fetchOfficialBom() {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			const project = await fetchHarvestBomFromRebrickable(
				currentBackendBaseUrl(),
				activeProject.project_id
			);
			updateProjectState(project);
			projectSuccess = `Fetched and froze official Rebrickable BOM revision ${project.bom?.bom_revision_id}.`;
		} catch (e: unknown) {
			projectError =
				e instanceof Error ? e.message : 'Failed to fetch the official Rebrickable BOM';
		} finally {
			projectBusy = false;
		}
	}

	async function saveProjectReview() {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			const mappings = JSON.parse(mappingJson) as Record<string, unknown>;
			const project = await updateHarvestReview(currentBackendBaseUrl(), activeProject.project_id, {
				policy: {
					...activeProject.policy,
					group_actions: groupActions,
					non_sortable_action: nonSortableAction
				},
				mappings,
				priority: projectPriority,
				match_policy: projectMatchPolicy,
				fallback_mode: projectFallbackMode
			});
			updateProjectState(project);
			projectSuccess = 'Review policy and matching rules saved.';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to save project review';
		} finally {
			projectBusy = false;
		}
	}

	function setCapacityAssignment(groupId: string, binId: string) {
		capacityAssignments = { ...capacityAssignments, [groupId]: binId };
	}

	async function planCapacity(useCustomAssignments = false) {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			const assignmentOverrides = useCustomAssignments
				? (activeProject.capacity_plan?.waves ?? []).flatMap((wave) =>
						wave.assignments.map((assignment) => ({
							group_id: assignment.group_id,
							bin_id: capacityAssignments[assignment.group_id] ?? assignment.bin_id
						}))
					)
				: undefined;
			updateProjectState(
				await planHarvestCapacity(
					currentBackendBaseUrl(),
					activeProject.project_id,
					assignmentOverrides
				)
			);
			projectSuccess = useCustomAssignments
				? 'Custom draft bin assignments saved without changing the live sorter.'
				: 'Assumed-empty capacity plan generated without changing live bin assignments.';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to plan capacity';
		} finally {
			projectBusy = false;
		}
	}

	async function checkBinReadiness() {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		projectSuccess = null;
		try {
			binReadiness = await fetchHarvestBinReadiness(
				currentBackendBaseUrl(),
				activeProject.project_id
			);
			projectSuccess =
				binReadiness.status === 'clear'
					? 'The suggested bins have no recorded assignments or contents.'
					: 'Recorded bin state checked; clearance is required before future live execution.';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to check bin readiness';
		} finally {
			projectBusy = false;
		}
	}

	async function runSimulation() {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		try {
			updateProjectState(
				await simulateHarvestProject(currentBackendBaseUrl(), activeProject.project_id)
			);
			projectSuccess = 'Quantity-aware routing simulation completed.';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to run simulation';
		} finally {
			projectBusy = false;
		}
	}

	async function simulateLedgerPiece() {
		if (!activeProject) return;
		projectBusy = true;
		projectError = null;
		try {
			const allocation = await proposeHarvestAllocation(
				currentBackendBaseUrl(),
				activeProject.project_id,
				{
					piece_id: simulatedPieceId,
					part_id: simulatedPartId,
					color_id: simulatedColorId
				}
			);
			await confirmHarvestAllocation(
				currentBackendBaseUrl(),
				activeProject.project_id,
				String(allocation.allocation_id)
			);
			updateProjectState(
				await fetchHarvestProject(currentBackendBaseUrl(), activeProject.project_id)
			);
			projectSuccess = `Confirmed simulated piece in ${String(allocation.group_id)}.`;
			simulatedPieceId = '';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to allocate simulated piece';
		} finally {
			projectBusy = false;
		}
	}

	async function undoAllocation(allocationId: string) {
		if (!activeProject || !undoReason.trim()) return;
		projectBusy = true;
		projectError = null;
		try {
			await undoHarvestAllocation(
				currentBackendBaseUrl(),
				activeProject.project_id,
				allocationId,
				undoReason.trim()
			);
			updateProjectState(
				await fetchHarvestProject(currentBackendBaseUrl(), activeProject.project_id)
			);
			projectSuccess = 'Allocation undone with an audit reason.';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to undo allocation';
		} finally {
			projectBusy = false;
		}
	}

	async function transitionProject(target: 'review' | 'paused' | 'completed') {
		if (!activeProject || !lifecycleReason.trim()) return;
		projectBusy = true;
		projectError = null;
		try {
			updateProjectState(
				await transitionHarvestProject(
					currentBackendBaseUrl(),
					activeProject.project_id,
					target,
					lifecycleReason.trim()
				)
			);
			projectSuccess = `Project moved to ${target}.`;
			lifecycleReason = '';
		} catch (e: unknown) {
			projectError = e instanceof Error ? e.message : 'Failed to change project state';
		} finally {
			projectBusy = false;
		}
	}

	async function previewDraft(draft: HarvestDraft) {
		previewingDraftId = draft.draft_id;
		draftsError = null;
		try {
			activeDraft = draft.groups
				? draft
				: await fetchHarvestDraft(currentBackendBaseUrl(), draft.draft_id);
		} catch (e: unknown) {
			draftsError = e instanceof Error ? e.message : 'Failed to load Harvest draft';
		} finally {
			previewingDraftId = null;
		}
	}

	async function handleBsxUpload(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;

		if (lookupResult?.status !== 'not_found') {
			uploadError = 'Run a LocaBriques lookup before uploading a manual bag plan.';
			return;
		}
		if (!file.name.toLowerCase().endsWith('.bsx')) {
			uploadError = 'Choose a BrickStore .bsx file downloaded from Bricks Per Bag.';
			return;
		}

		uploading = true;
		uploadError = null;
		uploadSuccess = null;
		try {
			const draft = await uploadHarvestBsxDraft(
				currentBackendBaseUrl(),
				lookupResult.set_number,
				file
			);
			activeDraft = draft;
			uploadSuccess = `Saved immutable draft ${draft.draft_id}.`;
			await loadDrafts(true);
		} catch (e: unknown) {
			uploadError = e instanceof Error ? e.message : 'Failed to import the BSX bag plan';
		} finally {
			uploading = false;
		}
	}

	async function importLocabriquesDraft() {
		if (lookupResult?.status !== 'found') return;
		locabriquesImporting = true;
		uploadError = null;
		uploadSuccess = null;
		try {
			const draft = await createLocabriquesDraft(currentBackendBaseUrl(), lookupResult.set_number);
			activeDraft = draft;
			uploadSuccess = `Saved immutable draft ${draft.draft_id}.`;
			await loadDrafts(true);
		} catch (e: unknown) {
			uploadError = e instanceof Error ? e.message : 'Failed to import the LocaBriques bag plan';
		} finally {
			locabriquesImporting = false;
		}
	}

	function lookupProblem(result: HarvestSourceLookup): string {
		return result.error ?? result.message ?? 'LocaBriques is currently unavailable.';
	}

	function formatInteger(value: number | null | undefined): string {
		return value == null ? '—' : value.toLocaleString();
	}

	function formatBytes(value: number | null | undefined): string {
		if (value == null) return '—';
		if (value < 1024) return `${value} B`;
		if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
		return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
	}

	function formatDate(value: string | null | undefined): string {
		if (!value) return 'Unknown';
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
	}

	function issueVariant(issue: HarvestValidationIssue): 'danger' | 'warning' | 'info' {
		if (issue.severity === 'error') return 'danger';
		if (issue.severity === 'warning') return 'warning';
		return 'info';
	}

	function nextProjectStep(project: HarvestProject): { title: string; detail: string } {
		const gates = project.readiness.gates;
		if (!project.bom || !gates.runtime_identifiers_ready) {
			return {
				title: 'Fetch the official set inventory',
				detail:
					'Use the Rebrickable button in step 1. The result is frozen as immutable source evidence.'
			};
		}
		if (!gates.review_policies_resolved) {
			return {
				title: 'Resolve the bag review choices',
				detail: 'Choose what to do with each non-numbered group in step 2, then save the review.'
			};
		}
		if (!gates.bom_reconciled) {
			return {
				title: 'Review the BOM differences',
				detail:
					'The bag plan and official BOM do not yet reconcile. Review the missing-parts report before continuing.'
			};
		}
		if (!gates.capacity_plan_ready) {
			return {
				title: 'Generate the capacity plan',
				detail:
					'Step 3 reads the current bins and creates inert build waves without reserving or moving anything.'
			};
		}
		if (!gates.simulation_passed) {
			return {
				title: 'Run the complete BOM simulation',
				detail: 'Step 4 checks every required quantity against the planned build waves.'
			};
		}
		if (!gates.physical_acceptance_passed) {
			return {
				title: 'Run the controlled physical test',
				detail: 'Enter the test evidence after the controlled validation run.'
			};
		}
		if (!gates.green_light_recorded) {
			return {
				title: 'Clear bins and record the green light',
				detail: 'Physically verify the planned bag and exception bins, then approve the exact revision.'
			};
		}
		if (!gates.live_activation_active && project.state !== 'completed') {
			return {
				title: 'Activate live bag sorting',
				detail: 'Pause the sorter, install the audited bin assignments, then resume from the global header.'
			};
		}
		return {
			title: project.state === 'completed' ? 'Live bag sorting complete' : 'Live bag sorting active',
			detail: project.state === 'completed' ? 'The official quantities are confirmed.' : 'Physical drops are being confirmed against bag quotas.'
		};
	}

	function summaryNextStep(project: HarvestProjectSummary): string {
		const gates = project.readiness.gates;
		if (!gates.authoritative_bom_frozen || !gates.runtime_identifiers_ready)
			return 'Confirm official inventory';
		if (!gates.review_policies_resolved || !gates.bom_reconciled) return 'Resolve BOM differences';
		if (!gates.capacity_plan_ready) return 'Build bin plan';
		if (!gates.simulation_passed) return 'Run simulation';
		if (!gates.physical_acceptance_passed) return 'Run controlled acceptance';
		if (!gates.green_light_recorded) return 'Clear bins and green-light';
		if (!gates.live_activation_active && project.state !== 'completed') return 'Activate live sorting';
		return project.state === 'completed' ? 'Complete' : 'Live sorting active';
	}

	function groupedProjectSets(): Array<{
		setNumber: string;
		recommended: HarvestProjectSummary;
		attempts: HarvestProjectSummary[];
	}> {
		const bySet = new Map<string, HarvestProjectSummary[]>();
		for (const project of projectSummaries) {
			bySet.set(project.set_number, [...(bySet.get(project.set_number) ?? []), project]);
		}
		const stateRank: Record<string, number> = {
			active: 10,
			completed: 9,
			approved: 8,
			accepted: 7,
			simulated: 6,
			ready: 5,
			review: 4,
			draft: 3,
			paused: 2
		};
		return [...bySet.entries()]
			.map(([setNumber, attempts]) => {
				const ordered = [...attempts].sort(
					(a, b) =>
						(stateRank[b.state] ?? 0) - (stateRank[a.state] ?? 0) ||
						b.updated_at.localeCompare(a.updated_at)
				);
				return { setNumber, recommended: ordered[0], attempts: ordered };
			})
			.sort((a, b) => b.recommended.updated_at.localeCompare(a.recommended.updated_at));
	}

	onMount(() => {
		if (manager.machines.size === 0) {
			manager.connect(`${getBackendWsBase()}/ws`);
		}
		void loadDrafts();
		void loadProjects();
	});

	$effect(() => {
		const nextBaseUrl = currentBackendBaseUrl();
		if (nextBaseUrl === activeBaseUrl) return;
		activeBaseUrl = nextBaseUrl;
		lookupResult = null;
		lookupError = null;
		uploadError = null;
		uploadSuccess = null;
		activeDraft = null;
		drafts = [];
		draftsError = null;
		projects = [];
		projectSummaries = [];
		activeProject = null;
		projectError = null;
		void loadDrafts();
		void loadProjects();
	});
</script>

<svelte:head><title>Project Harvest · Sorter</title></svelte:head>

<div class="min-h-screen bg-bg text-text">
	<AppHeader />

	<main class="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-6 sm:px-6">
		<header class="flex flex-wrap items-start justify-between gap-4">
			<div>
				<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
					Project Harvest
				</div>
				<h1 class="mt-1 text-2xl font-bold text-text">Build bag-plan drafts</h1>
				<p class="mt-1 max-w-3xl text-sm text-text-muted">
					Check LocaBriques for numbered-bag data. When it has no published inventory, download the
					whole-set BSX from Bricks Per Bag and preserve it as an immutable draft.
				</p>
			</div>
			<MachineDropdown />
		</header>

		<section class="border border-border bg-surface p-4">
			<div class="mb-4">
				<h2 class="text-base font-semibold text-text">Find bag information</h2>
				<p class="mt-1 text-sm text-text-muted">
					This draft-only step does not activate routing or reserve any bins.
				</p>
			</div>

			<form class="flex max-w-2xl flex-col gap-2 sm:flex-row" onsubmit={submitLookup}>
				<div class="min-w-0 flex-1">
					<Input
						id="harvest-set-number"
						type="search"
						placeholder="LEGO set number, for example 21369"
						bind:value={setNumber}
						oninput={resetLookupForInput}
					/>
				</div>
				<Button type="submit" loading={lookupLoading} disabled={!setNumber.trim()}>
					<Search class="h-4 w-4" />
					Check LocaBriques
				</Button>
			</form>

			{#if lookupError}
				<div class="mt-4"><Alert variant="danger">{lookupError}</Alert></div>
			{/if}

			{#if lookupResult?.status === 'found'}
				<div class="mt-4 flex flex-col gap-3">
					<Alert variant="success">
						<div class="font-semibold">LocaBriques bag information found</div>
						<div class="mt-1">
							{lookupResult.inventory?.name || `Set ${lookupResult.set_number}`}
							{#if lookupResult.inventory?.bag_count != null}
								· {lookupResult.inventory.bag_count} bag groups
							{/if}
							{#if lookupResult.inventory?.total_part_count != null}
								· {formatInteger(lookupResult.inventory.total_part_count)} parts
							{/if}
						</div>
						<div class="mt-1 text-text-muted">
							Save the published response as an immutable draft for BOM review. A manual BSX upload
							is not needed for this set.
						</div>
					</Alert>
					<div>
						<Button loading={locabriquesImporting} onclick={() => void importLocabriquesDraft()}>
							<FileCheck2 class="h-4 w-4" />
							Save LocaBriques draft
						</Button>
					</div>
				</div>
			{:else if lookupResult?.status === 'unavailable'}
				<div class="mt-4 flex flex-col gap-3">
					<Alert variant="danger">
						<div class="font-semibold">LocaBriques could not be checked</div>
						<div class="mt-1">{lookupProblem(lookupResult)}</div>
					</Alert>
					<div>
						<Button variant="secondary" size="sm" onclick={() => void runLookup()}
							>Retry lookup</Button
						>
					</div>
				</div>
			{:else if lookupResult?.status === 'ambiguous'}
				<div class="mt-4">
					<Alert variant="warning">
						<div class="font-semibold">More than one LocaBriques inventory matched</div>
						<div class="mt-1">
							Enter the exact set variant suffix, such as <span class="font-medium">-1</span>, and
							check again. Manual BSX import remains unavailable until the lookup returns a true
							not-found result.
						</div>
						{#if (lookupResult.candidates ?? []).length > 0}
							<ul class="mt-2 list-inside list-disc">
								{#each lookupResult.candidates ?? [] as candidate (candidate.set_num)}
									<li>
										<span class="font-medium">{candidate.set_num}</span>
										{#if candidate.name}
											· {candidate.name}{/if}
									</li>
								{/each}
							</ul>
						{/if}
					</Alert>
				</div>
			{:else if lookupResult?.status === 'not_found'}
				<div class="mt-4 flex flex-col gap-3">
					<Alert variant="warning">
						<div class="font-semibold">No published LocaBriques bag plan matched this set</div>
						<div class="mt-1">
							Check Bricks Per Bag for set {lookupResult.set_number}. If it is available, download
							the whole-set BSX and upload it below.
						</div>
					</Alert>

					<div class="flex flex-wrap items-center gap-2">
						<a
							href={bricksPerBagSetUrl(lookupResult.set_number)}
							target="_blank"
							rel="noopener noreferrer"
							class="inline-flex items-center gap-2 border border-border bg-surface px-3 py-1.5 text-sm font-medium text-text transition-colors hover:bg-bg"
						>
							<ExternalLink class="h-4 w-4" />
							Open Bricks Per Bag
						</a>
						<Button variant="secondary" loading={uploading} onclick={() => fileInput?.click()}>
							<FileUp class="h-4 w-4" />
							Upload downloaded BSX
						</Button>
						<input
							bind:this={fileInput}
							type="file"
							accept=".bsx,application/xml,text/xml"
							class="hidden"
							onchange={handleBsxUpload}
						/>
					</div>
				</div>
			{/if}

			{#if uploadError}
				<div class="mt-4"><Alert variant="danger">{uploadError}</Alert></div>
			{/if}
			{#if uploadSuccess}
				<div class="mt-4"><Alert variant="success">{uploadSuccess}</Alert></div>
			{/if}
		</section>

		{#if activeDraft}
			<section class="border border-border bg-surface">
				<header
					class="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3"
				>
					<div>
						<div class="flex items-center gap-2">
							<FileCheck2 class="h-5 w-5 text-success" />
							<h2 class="text-base font-semibold text-text">Immutable bag-plan draft</h2>
						</div>
						<div class="mt-1 text-sm text-text-muted">
							Set {activeDraft.set_number} · {activeDraft.source.filename} · saved {formatDate(
								activeDraft.created_at
							)}
						</div>
					</div>
					<span
						class="border border-info/40 bg-info/[0.08] px-2 py-1 text-xs font-semibold tracking-wider text-info uppercase"
					>
						Draft only
					</span>
				</header>

				<div
					class="grid grid-cols-2 divide-x divide-y divide-border border-b border-border sm:grid-cols-3 xl:grid-cols-6"
				>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Numbered bags
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatInteger(activeDraft.summary.numbered_bag_count)}
						</div>
					</div>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Bagged pieces
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatInteger(activeDraft.summary.numbered_quantity)}
						</div>
					</div>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Source pieces
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatInteger(activeDraft.summary.source_total_quantity)}
						</div>
					</div>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Part / color pairs
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatInteger(activeDraft.summary.unique_part_color_pairs)}
						</div>
					</div>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Other groups
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatInteger(activeDraft.summary.non_numbered_group_count)}
						</div>
					</div>
					<div class="p-3">
						<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
							Source size
						</div>
						<div class="mt-1 text-xl font-semibold tabular-nums">
							{formatBytes(activeDraft.source.size_bytes)}
						</div>
					</div>
				</div>

				<div class="flex flex-col gap-4 p-4">
					<Alert variant="info">
						The original file and SHA-256 revision are preserved. No sorter routing, project
						activation, or bin reservations were changed.
					</Alert>
					<div>
						<Button loading={projectBusy} onclick={() => void createProjectFromDraft()}>
							<ClipboardList class="h-4 w-4" />
							Create audited project from this draft
						</Button>
					</div>

					<div class="grid gap-3 text-sm md:grid-cols-3">
						<div class="border border-border bg-bg p-3">
							<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
								Structural validation
							</div>
							<div class="mt-1 font-medium text-text">
								{activeDraft.validation.structural_status}
							</div>
						</div>
						<div class="border border-border bg-bg p-3">
							<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
								BOM reconciliation
							</div>
							<div class="mt-1 font-medium text-text">{activeDraft.validation.bom_status}</div>
						</div>
						<div class="border border-border bg-bg p-3">
							<div class="text-xs font-semibold tracking-wider text-text-muted uppercase">
								Revision
							</div>
							<div class="mt-1 font-mono text-xs break-all text-text">
								{activeDraft.source.sha256}
							</div>
						</div>
					</div>

					{#if activeDraft.validation.issues.length > 0}
						<div class="flex flex-col gap-2">
							<h3 class="text-sm font-semibold text-text">Validation notes</h3>
							{#each activeDraft.validation.issues as issue, issueIndex (`${issue.code}-${issueIndex}`)}
								<Alert variant={issueVariant(issue)}>
									<span class="font-semibold">{issue.code}:</span>
									{issue.message}
								</Alert>
							{/each}
						</div>
					{/if}

					<div>
						<h3 class="mb-2 text-sm font-semibold text-text">Imported groups</h3>
						{#if (activeDraft.groups ?? []).length === 0}
							<div class="border border-border bg-bg px-3 py-4 text-sm text-text-muted">
								No detailed groups were returned for this draft.
							</div>
						{:else}
							<div class="overflow-x-auto border border-border">
								<table class="w-full text-sm">
									<thead
										class="bg-bg text-left text-xs font-semibold tracking-wider text-text-muted uppercase"
									>
										<tr>
											<th class="px-3 py-2">Group</th>
											<th class="px-3 py-2">Kind</th>
											<th class="px-3 py-2 text-right">Elements</th>
											<th class="px-3 py-2 text-right">Pieces</th>
										</tr>
									</thead>
									<tbody class="divide-y divide-border">
										{#each activeDraft.groups ?? [] as group (group.id)}
											<tr>
												<td class="px-3 py-2 font-medium text-text">{group.label}</td>
												<td class="px-3 py-2 text-text-muted">{group.kind}</td>
												<td class="px-3 py-2 text-right tabular-nums"
													>{formatInteger(group.distinct_elements)}</td
												>
												<td class="px-3 py-2 text-right tabular-nums"
													>{formatInteger(group.quantity)}</td
												>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
						{/if}
					</div>
				</div>
			</section>
		{/if}

		<section class="border border-border bg-surface p-4">
			<div class="mb-3 flex flex-wrap items-center justify-between gap-3">
				<div>
					<h2 class="text-base font-semibold text-text">Harvest project portfolio</h2>
					<p class="mt-1 text-sm text-text-muted">
						Frozen BOMs, review policy, capacity plans, simulation, progress, and audit history.
					</p>
				</div>
				<Button variant="secondary" size="sm" onclick={() => void loadProjects()}>Refresh</Button>
			</div>

			{#if projectError}
				<div class="mb-3"><Alert variant="danger">{projectError}</Alert></div>
			{/if}
			{#if projectSuccess}
				<div class="mb-3"><Alert variant="success">{projectSuccess}</Alert></div>
			{/if}

			{#if projectsLoading}
				<div class="py-4 text-sm text-text-muted">Loading Harvest projects…</div>
			{:else if projectSummaries.length === 0}
				<div class="border border-border bg-bg px-3 py-4 text-sm text-text-muted">
					Preview an immutable source draft and create its first audited project.
				</div>
			{:else}
				<div class="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
					{#each groupedProjectSets() as group (group.setNumber)}
						{@const project = group.recommended}
						<div class="border border-border bg-bg p-3">
							<div class="flex items-start gap-3">
								{#if project.set_metadata?.image_url}
									<img
										src={project.set_metadata.image_url}
										alt={`${project.set_metadata.name} set`}
										loading="lazy"
										class="h-20 w-20 flex-none border border-border bg-white object-contain p-1"
									/>
								{/if}
								<div class="min-w-0 flex-1">
									<div class="flex items-start justify-between gap-2">
										<div class="font-semibold text-text">
											{project.set_metadata?.name ?? project.name}
										</div>
										<span class="text-xs font-semibold tracking-wider text-text-muted uppercase"
											>{project.state}</span
										>
									</div>
									<div class="mt-1 text-sm text-text-muted">
										Set {project.set_number}{project.set_metadata
											? ` · ${project.set_metadata.year}`
											: ''}
									</div>
									{#if project.set_metadata}
										<div class="mt-1 text-sm text-text-muted">
											Official {formatInteger(project.set_metadata.official_piece_count)} pieces
											{#if project.bom}
												· <span
													class:text-success={project.bom.summary.total_quantity ===
														project.set_metadata.official_piece_count}
													class:text-warning={project.bom.summary.total_quantity !==
														project.set_metadata.official_piece_count}
												>
													{project.bom.summary.total_quantity ===
													project.set_metadata.official_piece_count
														? 'inventory matched'
														: `BOM ${formatInteger(project.bom.summary.total_quantity)}`}
												</span>
											{/if}
										</div>
									{/if}
									<div class="mt-2 text-xs text-text-muted">
										Priority {project.priority} · revision {project.revision} · BOM {project
											.reconciliation.status}
									</div>
									<div class="mt-1 text-xs font-medium text-info">
										Next: {summaryNextStep(project)}
									</div>
								</div>
							</div>
							<div class="mt-3 flex items-center justify-between gap-2 border-t border-border pt-3">
								<div class="text-xs text-text-muted">
									{group.attempts.length}
									{group.attempts.length === 1 ? 'attempt' : 'attempts'} ·
									{project.draft_source?.source_kind ??
										project.draft_source?.provider ??
										'saved draft'}
								</div>
								<a
									href={`/dashboard/harvest/projects/${encodeURIComponent(project.project_id)}`}
									class="inline-flex items-center border border-primary bg-primary px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90"
								>
									Resume most advanced
								</a>
							</div>
							{#if group.attempts.length > 1}
								<details class="mt-2">
									<summary class="cursor-pointer text-xs text-text-muted"
										>View earlier attempts</summary
									>
									<div class="mt-2 divide-y divide-border border border-border">
										{#each group.attempts as attempt (attempt.project_id)}
											<a
												href={`/dashboard/harvest/projects/${encodeURIComponent(attempt.project_id)}`}
												class="flex items-center justify-between gap-2 px-2 py-2 text-xs hover:bg-surface"
											>
												<span>Revision {attempt.revision} · {attempt.state}</span>
												<span class="text-text-muted">{summaryNextStep(attempt)}</span>
											</a>
										{/each}
									</div>
								</details>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</section>

		{#if activeProject}
			<section class="border border-border bg-surface">
				<header class="border-b border-border px-4 py-3">
					<div class="flex flex-wrap items-start justify-between gap-3">
						<div class="flex items-start gap-4">
							{#if activeProject.set_metadata?.image_url}
								<img
									src={activeProject.set_metadata.image_url}
									alt={`${activeProject.set_metadata.name} set`}
									class="h-24 w-24 flex-none border border-border bg-white object-contain p-1"
								/>
							{/if}
							<div>
								<div class="flex items-center gap-2">
									<ShieldCheck class="h-5 w-5 text-info" />
									<h2 class="text-base font-semibold text-text">
										{activeProject.set_metadata?.name ?? activeProject.name}
									</h2>
								</div>
								<div class="mt-1 text-sm text-text-muted">
									Set {activeProject.set_number}
									{#if activeProject.set_metadata}
										· {activeProject.set_metadata.year} · Official {formatInteger(
											activeProject.set_metadata.official_piece_count
										)} pieces
									{/if}
								</div>
								{#if activeProject.set_metadata?.set_url}
									<a
										href={activeProject.set_metadata.set_url}
										target="_blank"
										rel="noopener noreferrer"
										class="mt-1 inline-flex items-center gap-1 text-xs font-medium text-info underline"
									>
										View on Rebrickable <ExternalLink class="h-3 w-3" />
									</a>
								{/if}
								<div class="mt-1 font-mono text-xs text-text-muted">{activeProject.project_id}</div>
							</div>
						</div>
						<span
							class={activeProject.state === 'active'
								? 'border border-success/40 bg-success/[0.08] px-2 py-1 text-xs font-semibold tracking-wider text-success uppercase'
								: 'border border-info/40 bg-info/[0.08] px-2 py-1 text-xs font-semibold tracking-wider text-info uppercase'}
						>
							{activeProject.state === 'active'
								? 'Live Harvest routing active'
								: activeProject.state === 'completed'
									? 'Live Harvest run complete'
									: 'Guided activation workflow'}
						</span>
					</div>
				</header>

				<div class="mx-4 mt-4 border border-info/40 bg-info/[0.08] px-3 py-3">
					<div class="text-xs font-semibold tracking-wider text-info uppercase">Next step</div>
					<div class="mt-1 font-semibold text-text">{nextProjectStep(activeProject).title}</div>
					<div class="mt-1 text-sm text-text-muted">{nextProjectStep(activeProject).detail}</div>
				</div>

				<div class="grid gap-4 p-4 xl:grid-cols-2">
					<div class="flex flex-col gap-4">
						<div class="border border-border bg-bg p-3">
							<div class="mb-3 flex items-center gap-2">
								<FileCheck2 class="h-4 w-4 text-text-muted" />
								<h3 class="text-sm font-semibold text-text">1. Frozen authoritative BOM</h3>
							</div>
							{#if activeProject.bom}
								<div class="text-sm text-text">
									{activeProject.bom.filename} · {formatInteger(
										activeProject.bom.summary.total_quantity
									)} pieces · {formatInteger(activeProject.bom.summary.distinct_elements)} elements
								</div>
								<div class="mt-1 text-xs text-text-muted">
									{activeProject.bom.namespace.part} / {activeProject.bom.namespace.color}
								</div>
								<div class="mt-1 font-mono text-xs break-all text-text-muted">
									{activeProject.bom.source_sha256}
								</div>
							{:else}
								<div class="text-sm text-text-muted">
									Fetch the official inventory for set {activeProject.set_number}. Minifig parts are
									included; spare rows are preserved as evidence but excluded from the build
									quantity.
								</div>
							{/if}
							<div class="mt-3 flex flex-wrap items-center gap-2">
								<Button size="sm" loading={projectBusy} onclick={() => void fetchOfficialBom()}>
									<Search class="h-4 w-4" />
									{activeProject.bom
										? 'Refresh and freeze Rebrickable BOM'
										: 'Fetch and freeze Rebrickable BOM'}
								</Button>
								<a class="text-sm font-medium text-info underline" href="/settings/api-keys">
									Configure Rebrickable key
								</a>
							</div>
							<details class="mt-3 border-t border-border pt-3">
								<summary class="cursor-pointer text-sm font-medium text-text-muted">
									Advanced: upload a BOM file
								</summary>
								<div class="mt-3 flex flex-wrap items-center gap-2">
									<select
										class="setup-control px-2 py-1.5 text-sm text-text"
										bind:value={bomProvider}
									>
										<option value="private_moc">Private MOC / canonical</option>
										<option value="rebrickable">Rebrickable export</option>
									</select>
									<Button
										variant="secondary"
										size="sm"
										loading={projectBusy}
										onclick={() => bomInput?.click()}
									>
										<FileUp class="h-4 w-4" /> Upload and freeze BOM
									</Button>
									<input
										bind:this={bomInput}
										type="file"
										accept=".json,.csv,application/json,text/csv"
										class="hidden"
										onchange={handleBomUpload}
									/>
								</div>
							</details>
						</div>

						<div class="border border-border bg-bg p-3">
							<h3 class="text-sm font-semibold text-text">2. Reconciliation and review policy</h3>
							<div class="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
								<label class="text-xs text-text-muted">
									Priority
									<Input type="number" min="0" max="10000" step="1" bind:value={projectPriority} />
								</label>
								<label class="text-xs text-text-muted">
									Match policy
									<select
										class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
										bind:value={projectMatchPolicy}
									>
										<option value="exact">Exact only</option>
										<option value="compatible">Compatible</option>
										<option value="substitute">Substitutes allowed</option>
									</select>
								</label>
								<label class="text-xs text-text-muted">
									Fallback model
									<select
										class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
										bind:value={projectFallbackMode}
									>
										<option value="bag_plan">Bag plan</option>
										<option value="adaptive_part">Adaptive part groups</option>
										<option value="inventory_first">Inventory first</option>
									</select>
								</label>
								<label class="text-xs text-text-muted">
									Non-sortable BOM items
									<select
										class="setup-control mt-1 w-full px-2 py-2 text-sm text-text"
										bind:value={nonSortableAction}
									>
										<option value="exclude">Exclude with count</option>
										<option value="include">Include in groups</option>
										<option value="review">Require review</option>
									</select>
								</label>
							</div>
							<div class="mt-3 divide-y divide-border border border-border">
								{#each Object.entries(groupActions) as [groupId, action] (groupId)}
									<label class="flex items-center justify-between gap-3 px-2 py-2 text-sm">
										<span class="font-mono text-xs text-text">{groupId}</span>
										<select
											class="setup-control px-2 py-1 text-sm text-text"
											value={action}
											onchange={(event) =>
												(groupActions = {
													...groupActions,
													[groupId]: event.currentTarget.value
												})}
										>
											<option value="separate">Separate destination</option>
											<option value="include">Include</option>
											<option value="exclude">Exclude</option>
											<option value="review">Needs review</option>
										</select>
									</label>
								{/each}
							</div>
							<details class="mt-3">
								<summary class="cursor-pointer text-xs font-medium text-text-muted">
									Advanced namespace mappings and substitute rules
								</summary>
								<textarea
									aria-label="Namespace mappings and compatible or substitute rules"
									class="setup-control mt-2 min-h-40 w-full p-2 font-mono text-xs text-text"
									bind:value={mappingJson}
								></textarea>
							</details>
							<div class="mt-3 flex flex-wrap items-center justify-between gap-2">
								<div class="text-sm text-text-muted">
									Status: <span class="font-semibold text-text"
										>{activeProject.reconciliation.status}</span
									>
									{#if activeProject.reconciliation.unmapped.length > 0}
										· {activeProject.reconciliation.unmapped.length} unmapped
									{/if}
								</div>
								<Button size="sm" loading={projectBusy} onclick={() => void saveProjectReview()}
									>Save review</Button
								>
							</div>
						</div>
					</div>

					<div class="flex flex-col gap-4">
						<div class="border border-border bg-bg p-3">
							<div class="flex items-center gap-2">
								<Layers3 class="h-4 w-4 text-text-muted" />
								<h3 class="text-sm font-semibold text-text">3. Capacity and build waves</h3>
							</div>
							<div class="mt-2 text-sm text-text-muted">
								Draft planning assumes every enabled bin is empty. Current assignments and recorded
								contents are retained as a later clearance warning and are never changed here.
							</div>
							{#if activeProject.capacity_plan}
								<div class="mt-2 text-sm text-text">
									{activeProject.capacity_plan.status} · {activeProject.capacity_plan
										.execution_mode} ·
									{activeProject.capacity_plan.summary?.wave_count ?? 0} wave(s)
								</div>
								{#if (activeProject.capacity_plan.summary?.planned_bins_requiring_clearance ?? 0) > 0}
									<div class="mt-2 border border-warning/40 bg-warning/10 p-2 text-xs text-text">
										{activeProject.capacity_plan.summary?.planned_bins_requiring_clearance}
										suggested bin(s) currently have assignments or recorded contents. This does not block
										draft simulation, but they must be cleared before live execution.
									</div>
								{/if}
								<div class="mt-2 flex flex-col gap-2">
									{#each activeProject.capacity_plan.waves as wave (wave.wave)}
										<div class="border border-border bg-surface p-2 text-xs">
											<div class="font-semibold">Wave {wave.wave}</div>
											<div class="mt-1 flex flex-col gap-1">
												{#each wave.assignments as item (`${wave.wave}-${item.group_id}`)}
													<div class="grid items-center gap-2 sm:grid-cols-[minmax(0,1fr)_10rem]">
														<span>{item.group_label} · {formatInteger(item.quantity)} pieces</span>
														{#if editingCapacityAssignments}
															<select
																class="setup-control w-full p-1 text-xs text-text"
																value={capacityAssignments[item.group_id] ?? item.bin_id}
																onchange={(event) =>
																	setCapacityAssignment(item.group_id, event.currentTarget.value)}
															>
																{#each activeProject.capacity_plan?.bins.filter((bin) => bin.available) ?? [] as bin (bin.bin_id)}
																	<option value={bin.bin_id}>{bin.bin_id}</option>
																{/each}
															</select>
														{:else}
															<span class="font-mono">{item.bin_id}</span>
														{/if}
													</div>
												{/each}
											</div>
										</div>
									{/each}
								</div>
								{#if activeProject.capacity_plan.status === 'ready'}
									<div class="mt-3 flex flex-wrap gap-2">
										{#if editingCapacityAssignments}
											<Button
												size="sm"
												loading={projectBusy}
												onclick={() => void planCapacity(true)}
											>
												Save custom assignments
											</Button>
											<Button
												variant="secondary"
												size="sm"
												onclick={() => (editingCapacityAssignments = false)}
											>
												Cancel
											</Button>
										{:else}
											<Button
												variant="secondary"
												size="sm"
												onclick={() => (editingCapacityAssignments = true)}
											>
												Customize bin assignments
											</Button>
										{/if}
									</div>
								{/if}
							{:else}
								<div class="mt-2 text-sm text-text-muted">
									No assumed-empty capacity plan has been generated for this revision.
								</div>
							{/if}
							<div class="mt-3">
								<Button
									variant="secondary"
									size="sm"
									loading={projectBusy}
									disabled={!activeProject.readiness.gates.bom_reconciled ||
										!activeProject.readiness.gates.review_policies_resolved}
									onclick={() => void planCapacity(false)}
								>
									<Layers3 class="h-4 w-4" /> Build assumed-empty plan
								</Button>
							</div>
						</div>

						<div class="border border-border bg-bg p-3">
							<div class="flex items-center gap-2">
								<Play class="h-4 w-4 text-text-muted" />
								<h3 class="text-sm font-semibold text-text">4. Quantity-aware simulation</h3>
							</div>
							<div class="mt-2 text-sm text-text-muted">
								{#if activeProject.simulation}
									{activeProject.simulation.status} · routed {formatInteger(
										activeProject.simulation.summary.routed_quantity
									)} · missing {formatInteger(activeProject.simulation.summary.missing_quantity)}
								{:else}
									No simulation has been recorded for this revision.
								{/if}
							</div>
							<div class="mt-3">
								<Button
									size="sm"
									loading={projectBusy}
									disabled={!activeProject.readiness.gates.capacity_plan_ready}
									onclick={() => void runSimulation()}
								>
									<Play class="h-4 w-4" /> Run complete BOM simulation
								</Button>
							</div>
							{#if activeProject.readiness.gates.simulation_passed}
								<div class="mt-4 border-t border-border pt-3">
									<div class="text-sm font-semibold text-text">Execution bin preflight</div>
									<div class="mt-1 text-xs text-text-muted">
										Checks recorded assignments and piece counts only. Physically verify the
										selected bins before clearing their records in Bin Management.
									</div>
									<div class="mt-2 flex flex-wrap gap-2">
										<Button
											variant="secondary"
											size="sm"
											loading={projectBusy}
											onclick={() => void checkBinReadiness()}
										>
											Check actual bin status
										</Button>
										<a
											href="/bins"
											class="inline-flex items-center gap-2 border border-border bg-surface px-2.5 py-1 text-xs font-medium text-text hover:bg-bg"
										>
											Open Bin Management <ExternalLink class="h-3 w-3" />
										</a>
									</div>
									{#if binReadiness}
										<div class="mt-2 border border-border bg-surface p-2 text-xs text-text">
											<div class="font-semibold">
												{binReadiness.status === 'clear'
													? 'Suggested bins are clear in recorded state.'
													: `${binReadiness.suggested_bins_requiring_clearance.length} suggested bin(s) require clearance.`}
											</div>
											{#if binReadiness.status !== 'clear'}
												<div class="mt-2 flex flex-wrap gap-2">
													<Button
														variant={clearanceScope === 'suggested' ? 'primary' : 'secondary'}
														size="sm"
														onclick={() => (clearanceScope = 'suggested')}
													>
														Suggested bins ({binReadiness.suggested_bins_requiring_clearance
															.length})
													</Button>
													<Button
														variant={clearanceScope === 'all' ? 'primary' : 'secondary'}
														size="sm"
														onclick={() => (clearanceScope = 'all')}
													>
														All non-clear bins ({binReadiness.all_bins_requiring_clearance.length})
													</Button>
												</div>
												<div class="mt-2 font-mono text-[11px] text-text-muted">
													{(clearanceScope === 'suggested'
														? binReadiness.suggested_bins_requiring_clearance
														: binReadiness.all_bins_requiring_clearance
													)
														.map((bin) => bin.bin_id)
														.join(', ')}
												</div>
											{/if}
										</div>
									{/if}
								</div>
							{/if}
						</div>

						<div class="border border-border bg-bg p-3">
							<div class="flex items-center gap-2">
								<Route class="h-4 w-4 text-text-muted" />
								<h3 class="text-sm font-semibold text-text">5. Durable simulated piece ledger</h3>
							</div>
							<div class="mt-2 grid gap-2 sm:grid-cols-3">
								<Input placeholder="Piece id" bind:value={simulatedPieceId} />
								<Input placeholder="Part id" bind:value={simulatedPartId} />
								<Input placeholder="Color id" bind:value={simulatedColorId} />
							</div>
							<div class="mt-3 flex flex-wrap items-center justify-between gap-2">
								<div class="text-sm text-text-muted">
									{formatInteger(activeProject.progress.summary.confirmed_quantity)} / {formatInteger(
										activeProject.progress.summary.required_quantity
									)} confirmed in simulation
								</div>
								<Button
									variant="secondary"
									size="sm"
									loading={projectBusy}
									disabled={!activeProject.readiness.gates.simulation_passed ||
										!simulatedPieceId ||
										!simulatedPartId ||
										!simulatedColorId}
									onclick={() => void simulateLedgerPiece()}
								>
									Route and confirm simulated piece
								</Button>
							</div>
							{#if (activeProject.allocations ?? []).length > 0}
								<div class="mt-3 border-t border-border pt-3">
									<label class="block text-xs text-text-muted">
										Required reason for undo
										<Input bind:value={undoReason} />
									</label>
									<div
										class="mt-2 max-h-44 divide-y divide-border overflow-auto border border-border"
									>
										{#each (activeProject.allocations ?? [])
											.slice()
											.reverse()
											.slice(0, 8) as allocation (allocation.allocation_id)}
											<div class="flex items-center justify-between gap-2 p-2 text-xs">
												<div>
													<div class="font-semibold text-text">
														{allocation.piece_id} → {allocation.group_id}
													</div>
													<div class="text-text-muted">
														{allocation.part_id} / {allocation.color_id} · {allocation.status} · {allocation.match_kind}
													</div>
												</div>
												{#if allocation.status !== 'undone'}
													<Button
														variant="ghost"
														size="sm"
														disabled={projectBusy || !undoReason.trim()}
														onclick={() => void undoAllocation(allocation.allocation_id)}
													>
														Undo
													</Button>
												{/if}
											</div>
										{/each}
									</div>
								</div>
							{/if}
						</div>

						<div class="border border-border bg-bg p-3">
							<h3 class="text-sm font-semibold text-text">6. Readiness and missing parts</h3>
							<div class="mt-2 grid grid-cols-1 gap-1 text-sm sm:grid-cols-2">
								{#each Object.entries(activeProject.readiness.gates) as [gate, passed] (gate)}
									<div
										class="flex items-center justify-between gap-2 border border-border bg-surface px-2 py-1.5"
									>
										<span class="text-text-muted">{gate.replaceAll('_', ' ')}</span>
										<span
											class:text-success={passed}
											class:text-danger={!passed}
											class="font-semibold"
										>
											{passed ? 'pass' : 'blocked'}
										</span>
									</div>
								{/each}
							</div>
							<a
								class="mt-3 inline-flex items-center gap-2 border border-border bg-surface px-3 py-1.5 text-sm font-medium text-text hover:bg-bg"
								href={missingPartsCsvUrl(currentBackendBaseUrl(), activeProject.project_id)}
								download
							>
								<Download class="h-4 w-4" /> Export missing parts CSV
							</a>
						</div>
					</div>
				</div>

				<div class="grid gap-4 border-t border-border p-4 xl:grid-cols-2">
					<div class="border border-border bg-bg p-3">
						<h3 class="text-sm font-semibold text-text">Controlled physical acceptance</h3>
						<p class="mt-1 text-sm text-text-muted">
							Use the guided project workspace to arm a bounded routing test, auto-pause at the
							piece limit, inspect the actual destinations, and record sorter-captured evidence.
						</p>
						<a
							class="mt-3 inline-flex items-center gap-2 border border-border bg-surface px-3 py-1.5 text-sm font-medium text-text hover:bg-bg"
							href={`/dashboard/harvest/projects/${encodeURIComponent(activeProject.project_id)}`}
						>
							<ShieldCheck class="h-4 w-4" /> Open guided Step 5
						</a>
						<div class="mt-4 border-t border-border pt-3">
							<div class="text-sm font-semibold text-text">Audited project lifecycle</div>
							<div class="mt-1 text-xs text-text-muted">
								Current state: {activeProject.state}. Every manual state change requires a reason.
							</div>
							<div class="mt-2 flex flex-wrap items-end gap-2">
								<label class="min-w-64 flex-1 text-xs text-text-muted">
									Transition reason
									<Input bind:value={lifecycleReason} />
								</label>
								{#if activeProject.state === 'paused' || activeProject.state === 'completed'}
									<Button
										variant="secondary"
										size="sm"
										disabled={projectBusy || !lifecycleReason.trim()}
										onclick={() => void transitionProject('review')}
									>
										Reopen in review
									</Button>
								{:else}
									<Button
										variant="secondary"
										size="sm"
										disabled={projectBusy || !lifecycleReason.trim()}
										onclick={() => void transitionProject('paused')}
									>
										Pause
									</Button>
								{/if}
								{#if activeProject.state === 'accepted'}
									<Button
										size="sm"
										disabled={projectBusy ||
											!lifecycleReason.trim() ||
											activeProject.progress.summary.missing_quantity > 0}
										onclick={() => void transitionProject('completed')}
									>
										Complete
									</Button>
								{/if}
							</div>
						</div>
					</div>

					<div class="border border-border bg-bg p-3">
						<h3 class="text-sm font-semibold text-text">Tamper-evident audit history</h3>
						<div class="mt-2 max-h-60 divide-y divide-border overflow-auto border border-border">
							{#each (activeProject.events ?? [])
								.slice()
								.reverse()
								.slice(0, 12) as event (event.event_id)}
								<div class="p-2 text-xs">
									<div class="flex items-center justify-between gap-2">
										<span class="font-semibold text-text">#{event.sequence} {event.kind}</span>
										<span class="text-text-muted">{formatDate(event.created_at)}</span>
									</div>
									<div class="mt-1 font-mono break-all text-text-muted">{event.event_hash}</div>
								</div>
							{/each}
						</div>
					</div>
				</div>
			</section>
		{/if}

		<section class="border border-border bg-surface p-4">
			<div class="mb-3 flex flex-wrap items-center justify-between gap-3">
				<div>
					<h2 class="text-base font-semibold text-text">Saved Harvest drafts</h2>
					<p class="mt-1 text-sm text-text-muted">
						Immutable manual imports stored on this machine.
					</p>
				</div>
				<Button variant="secondary" size="sm" onclick={() => void loadDrafts()}>Refresh</Button>
			</div>

			{#if draftsError}
				<Alert variant="danger">{draftsError}</Alert>
			{:else if draftsLoading}
				<div class="py-4 text-sm text-text-muted">Loading Harvest drafts…</div>
			{:else if drafts.length === 0}
				<div class="border border-border bg-bg px-3 py-4 text-sm text-text-muted">
					No manual bag-plan drafts have been uploaded yet.
				</div>
			{:else}
				<div class="divide-y divide-border border border-border">
					{#each drafts as draft (draft.draft_id)}
						<div class="flex flex-wrap items-center justify-between gap-3 bg-bg px-3 py-3">
							<div class="min-w-0">
								<div class="text-sm font-semibold text-text">Set {draft.set_number}</div>
								<div class="mt-1 text-sm text-text-muted">
									{draft.source.filename} · {formatInteger(draft.summary.numbered_bag_count)} numbered
									bags · {formatInteger(draft.summary.source_total_quantity)} pieces
								</div>
								<div class="mt-1 font-mono text-xs text-text-muted">{draft.draft_id}</div>
							</div>
							<Button
								variant="secondary"
								size="sm"
								loading={previewingDraftId === draft.draft_id}
								onclick={() => void previewDraft(draft)}
							>
								Preview
							</Button>
						</div>
					{/each}
				</div>
			{/if}
		</section>
	</main>
</div>
