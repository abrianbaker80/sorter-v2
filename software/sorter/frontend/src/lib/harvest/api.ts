import type { PieceSummary } from '$lib/pieces';

export type HarvestSourceStatus = 'found' | 'not_found' | 'unavailable' | 'ambiguous';

export type HarvestSourceInventory = {
	set_num: string;
	name?: string | null;
	year?: number | null;
	image_url?: string | null;
	bag_count?: number | null;
	total_part_count?: number | null;
	api_url?: string | null;
	website_url?: string | null;
};

export type HarvestSourceLookup = {
	status: HarvestSourceStatus;
	set_number: string;
	inventory?: HarvestSourceInventory | null;
	candidates?: HarvestSourceInventory[];
	error?: string | null;
	message?: string | null;
};

export type HarvestValidationIssue = {
	code: string;
	severity: 'info' | 'warning' | 'error' | string;
	message: string;
	details?: Record<string, unknown>;
};

export type HarvestDraftSource = {
	filename: string;
	sha256: string;
	size_bytes: number;
	source_kind?: string | null;
	provider?: string | null;
	stored_filename?: string | null;
	source_url?: string | null;
	bricksperbag_url?: string | null;
	website_url?: string | null;
	published_on?: string | null;
	author_username?: string | null;
};

export type HarvestDraftValidation = {
	structural_status: string;
	bom_status: string;
	activation_allowed?: boolean;
	activation_status?: string | null;
	issues: HarvestValidationIssue[];
};

export type HarvestDraftPart = {
	item_type: string;
	item_id: string;
	color_id: string;
	quantity: number;
	condition?: string | null;
	status?: string | null;
	source_rows?: number[];
};

export type HarvestDraftGroup = {
	id: string;
	kind: 'numbered' | 'unnumbered' | 'extras' | string;
	bag_number?: number | null;
	label: string;
	source_labels?: string[];
	source_row_count?: number;
	quantity: number;
	distinct_elements: number;
	parts?: HarvestDraftPart[];
};

export type HarvestDraftSummary = {
	source_row_count?: number;
	source_total_quantity?: number;
	numbered_bag_count?: number;
	numbered_quantity?: number;
	non_numbered_group_count?: number;
	non_numbered_quantity?: number;
	unique_item_ids?: number;
	unique_part_color_pairs?: number;
	duplicate_rows_merged?: number;
	required_numbered_bins?: number;
	additional_groups_requiring_policy?: number;
	[key: string]: number | undefined;
};

export type HarvestDraft = {
	draft_id: string;
	set_number: string;
	created_at?: string | null;
	bricksperbag_url?: string | null;
	source: HarvestDraftSource;
	validation: HarvestDraftValidation;
	summary: HarvestDraftSummary;
	groups?: HarvestDraftGroup[];
};

export type HarvestBom = {
	bom_revision_id: string;
	provider: string;
	filename: string;
	source_sha256: string;
	normalized_sha256: string;
	namespace: { part: string; color: string };
	summary: {
		source_row_count: number;
		distinct_elements: number;
		total_quantity: number;
		sortable_quantity: number;
		non_sortable_quantity: number;
	};
	set_metadata?: HarvestSetMetadata | null;
	runtime_aliases?: {
		status: 'ready' | 'incomplete' | string;
		summary: {
			alias_count: number;
			covered_quantity: number;
			missing_quantity: number;
			sortable_quantity: number;
		};
	};
};

export type HarvestSetMetadata = {
	set_number: string;
	name: string;
	year: number;
	theme_id: number;
	official_piece_count: number;
	image_url?: string | null;
	set_url?: string | null;
	last_modified_at?: string | null;
	source_bom_revision_id?: string;
	snapshot_created_at?: string;
};

export type HarvestReconciliation = {
	status: string;
	issues: Array<Record<string, unknown>>;
	missing: Array<Record<string, unknown>>;
	overages: Array<Record<string, unknown>>;
	extras?: Array<Record<string, unknown>>;
	unmapped: Array<{
		group_id: string;
		part_id: string;
		color_id: string;
		quantity: number;
	}>;
	summary?: {
		bom_quantity: number;
		planned_quantity: number;
		extras_quantity: number;
		missing_quantity: number;
		overage_quantity: number;
	};
};

export type HarvestCapacityPlan = {
	status: string;
	execution_mode: string;
	planning_assumption?: string;
	selection_strategy?: string;
	issues: Array<Record<string, unknown>>;
	summary?: {
		required_groups: number;
		available_bins: number;
		wave_count: number;
		reserved_bins_excluded: number;
		reserved_bins_observed?: number;
		planned_bins_requiring_clearance?: number;
	};
	clearance?: {
		status: 'clear' | 'required' | string;
		bins: Array<{
			bin_id: string;
			reserved_by: string[];
			tracked_piece_count: number;
		}>;
	};
	assignment_overrides?: Array<{ group_id: string; bin_id: string }>;
	exception_destination?: {
		group_id: string;
		group_label: string;
		quantity: number;
		bin_id: string;
	} | null;
	overflow?: Array<Record<string, unknown>>;
	bins: Array<{
		bin_id: string;
		available: boolean;
		reserved_by: string[];
		max_pieces?: number | null;
		tracked_piece_count?: number;
	}>;
	waves: Array<{
		wave: number;
		assignments: Array<{
			group_id: string;
			group_label: string;
			quantity: number;
			bin_id: string;
		}>;
	}>;
};

export type HarvestBinReadiness = {
	status: 'clear' | 'clearance_required' | string;
	project_id: string;
	planning_assumption: string;
	suggested_bin_count: number;
	planned_bin_ids: string[];
	suggested_bins_requiring_clearance: Array<{
		bin_id: string;
		layer_index?: number;
		section_index?: number;
		bin_index?: number;
		reserved_by: string[];
		tracked_piece_count: number;
	}>;
	all_bins_requiring_clearance: Array<{
		bin_id: string;
		layer_index?: number;
		section_index?: number;
		bin_index?: number;
		reserved_by: string[];
		tracked_piece_count: number;
	}>;
	bin_state_token: string;
	recorded_state_only: boolean;
	physical_verification_required: boolean;
	record_clearance_allowed: boolean;
	live_changes_allowed: boolean;
};

export type HarvestAllocation = {
	allocation_id: string;
	project_id: string;
	runtime_id?: string | null;
	piece_id: string;
	group_id: string;
	part_id: string;
	color_id: string;
	quantity: number;
	match_kind: string;
	mode: string;
	status: 'planned' | 'confirmed' | 'undone' | string;
	created_at: string;
	confirmed_at?: string | null;
	undone_at?: string | null;
};

export type HarvestAcceptancePiece = {
	sequence: number;
	allocation_id: string;
	piece_id: string;
	group_id: string;
	group_label: string;
	bin_id?: string | null;
	display_bin_id?: string | null;
	destination_bin?: [number, number, number] | null;
	confirmed_at?: string | null;
	exception: boolean;
	summary: PieceSummary;
};

export type HarvestAcceptancePiecesResponse = {
	project_id: string;
	acceptance_run_id?: string | null;
	piece_count: number;
	items: HarvestAcceptancePiece[];
};

export type HarvestPartProgress = {
	part_id: string;
	color_id: string;
	required: number;
	confirmed: number;
	missing: number;
};

export type HarvestBagProgress = {
	group_id: string;
	label: string;
	required: number;
	confirmed: number;
	complete: boolean;
	parts: HarvestPartProgress[];
};

export type HarvestProject = {
	project_id: string;
	draft_id: string;
	set_number: string;
	name: string;
	state: string;
	priority: number;
	match_policy: 'exact' | 'compatible' | 'substitute' | string;
	fallback_mode: 'bag_plan' | 'adaptive_part' | 'inventory_first' | string;
	revision: number;
	created_at: string;
	updated_at: string;
	policy: {
		group_actions: Record<string, 'separate' | 'include' | 'exclude' | 'review' | string>;
		non_sortable_action: string;
		extras_action: string;
		unknown_classification_action: string;
		physical_confirmation_required: boolean;
	};
	mappings: {
		namespace: { parts: Record<string, string>; colors: Record<string, string> };
		substitutions: Array<Record<string, unknown>>;
	};
	bom?: HarvestBom | null;
	set_metadata?: HarvestSetMetadata | null;
	reconciliation: HarvestReconciliation;
	capacity_plan?: HarvestCapacityPlan | null;
	simulation?: {
		run_id: string;
		status: string;
		created_at: string;
		summary: Record<string, number>;
		missing: Array<Record<string, unknown>>;
		surplus: Array<Record<string, unknown>>;
	} | null;
	acceptance?: Record<string, unknown> | null;
	green_light?: {
		green_light_id: string;
		operator: string;
		reason: string;
		bin_ids: string[];
		recorded_at: string;
		motion_enabled: false;
	} | null;
	activation?: HarvestActivation | null;
	draft_source?: {
		provider?: string | null;
		source_kind?: string | null;
		filename?: string | null;
		created_at?: string | null;
	} | null;
	effective_groups: Array<{
		id: string;
		label: string;
		kind: string;
		quantity: number;
	}>;
	progress: {
		summary: {
			required_quantity: number;
			confirmed_quantity: number;
			missing_quantity: number;
			complete_groups: number;
			group_count: number;
		};
		groups: HarvestBagProgress[];
		missing: Array<
			HarvestPartProgress & {
				group_id: string;
				group_label: string;
			}
		>;
	};
	readiness: {
		gates: Record<string, boolean>;
		draft_ready: boolean;
		ready_for_physical_acceptance: boolean;
		ready_for_green_light: boolean;
		green_light_approved: boolean;
		activation_eligible: boolean;
		live_integration_enabled: boolean;
		hardware_activation_allowed: boolean;
		release_gate: string;
	};
	allocations?: HarvestAllocation[];
	allocation_summary?: {
		total: number;
		returned: number;
		by_status: Record<string, number>;
		exception_total: number;
		exception_confirmed: number;
	};
	events?: Array<{
		event_id: number;
		sequence: number;
		kind: string;
		actor: string;
		payload: Record<string, unknown>;
		event_hash: string;
		created_at: string;
	}>;
};

export type HarvestActivation = {
	activation_id: string;
	runtime_mode?: 'live' | 'acceptance' | string;
	status: 'active' | 'awaiting_observation' | 'stopped' | 'completed' | string;
	project_id: string;
	operator: string;
	reason?: string;
	activated_at?: string;
	armed_at?: string;
	test_id?: string;
	test_piece_limit?: number;
	confirmed_piece_count?: number;
	observed_routes?: Array<{
		group_id: string;
		group_label: string;
		bin_id?: string | null;
		piece_count: number;
	}>;
	assignments: Array<{
		group_id: string;
		group_label: string;
		bin_id: string;
		category_id: string;
		layer_index: number;
		section_index: number;
		bin_index: number;
	}>;
	progress?: HarvestProject['progress'];
	runtime_progress?: {
		planned_piece_count: number;
		confirmed_piece_count: number;
		bag_confirmed_piece_count?: number;
		exception_confirmed_piece_count?: number;
		exception_planned_piece_count?: number;
		test_piece_limit?: number | null;
	};
};

export type HarvestRuntimeStatus = {
	active: (HarvestActivation & {
		set_number: string;
		name: string;
		allocation_summary: HarvestProject['allocation_summary'];
	}) | null;
	sorter_state: string | null;
	acceptance_evidence_recording_allowed: boolean;
	activation_requires_paused_sorter: boolean;
	activation_starts_motion: boolean;
};

export type HarvestProjectSummary = Pick<
	HarvestProject,
	| 'project_id'
	| 'draft_id'
	| 'set_number'
	| 'name'
	| 'state'
	| 'priority'
	| 'revision'
	| 'created_at'
	| 'updated_at'
	| 'draft_source'
	| 'set_metadata'
	| 'readiness'
> & {
	bom?: { provider: string; summary: HarvestBom['summary'] } | null;
	reconciliation: {
		status: string;
		summary?: HarvestReconciliation['summary'];
		issue_count: number;
		unmapped_count: number;
	};
	capacity_plan?: {
		status: string;
		summary?: HarvestCapacityPlan['summary'];
	} | null;
	progress: HarvestProject['progress']['summary'];
};

type JsonError = {
	detail?: string | { code?: string; message?: string };
	error?: string;
	message?: string;
};

export class HarvestApiError extends Error {
	constructor(message: string, public code?: string) {
		super(message);
	}
}

async function unwrap<T>(response: Response): Promise<T> {
	if (!response.ok) {
		const body = (await response.json().catch(() => null)) as JsonError | null;
		const detail = body?.detail;
		const detailMessage =
			typeof detail === 'string'
				? detail
				: detail && typeof detail.message === 'string'
					? detail.message
					: null;
		throw new HarvestApiError(
			detailMessage ?? body?.error ?? body?.message ?? `HTTP ${response.status}`,
			typeof detail === 'object' && detail ? detail.code : undefined
		);
	}
	return (await response.json()) as T;
}

export function bricksPerBagSetUrl(setNumber: string): string {
	const baseSetNumber = setNumber.trim().split('-', 1)[0];
	return `https://bricksperbag.com/set/${encodeURIComponent(baseSetNumber)}`;
}

export async function lookupLocabriques(
	baseUrl: string,
	setNumber: string
): Promise<HarvestSourceLookup> {
	return unwrap<HarvestSourceLookup>(
		await fetch(
			`${baseUrl}/api/project-harvest/sources/locabriques/${encodeURIComponent(setNumber)}`
		)
	);
}

export async function uploadHarvestBsxDraft(
	baseUrl: string,
	setNumber: string,
	file: File
): Promise<HarvestDraft> {
	const query = new URLSearchParams({ set_number: setNumber, filename: file.name });
	return unwrap<HarvestDraft>(
		await fetch(`${baseUrl}/api/project-harvest/drafts/bsx?${query.toString()}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/xml' },
			body: file
		})
	);
}

export async function createLocabriquesDraft(
	baseUrl: string,
	setNumber: string
): Promise<HarvestDraft> {
	return unwrap<HarvestDraft>(
		await fetch(
			`${baseUrl}/api/project-harvest/drafts/locabriques/${encodeURIComponent(setNumber)}`,
			{ method: 'POST' }
		)
	);
}

export async function fetchHarvestDrafts(baseUrl: string): Promise<HarvestDraft[]> {
	const payload = await unwrap<{ drafts: HarvestDraft[] }>(
		await fetch(`${baseUrl}/api/project-harvest/drafts`)
	);
	return payload.drafts;
}

export async function fetchHarvestDraftSummaries(baseUrl: string): Promise<HarvestDraft[]> {
	const payload = await unwrap<{ drafts: HarvestDraft[] }>(
		await fetch(`${baseUrl}/api/project-harvest/drafts/summaries`)
	);
	return payload.drafts;
}

export async function fetchHarvestDraft(baseUrl: string, draftId: string): Promise<HarvestDraft> {
	return unwrap<HarvestDraft>(
		await fetch(`${baseUrl}/api/project-harvest/drafts/${encodeURIComponent(draftId)}`)
	);
}

export async function fetchHarvestProjects(baseUrl: string): Promise<HarvestProject[]> {
	const payload = await unwrap<{ projects: HarvestProject[] }>(
		await fetch(`${baseUrl}/api/project-harvest/projects`)
	);
	return payload.projects;
}

export async function fetchHarvestProjectSummaries(
	baseUrl: string
): Promise<HarvestProjectSummary[]> {
	const payload = await unwrap<{ projects: HarvestProjectSummary[] }>(
		await fetch(`${baseUrl}/api/project-harvest/projects/summaries`)
	);
	return payload.projects;
}

export async function fetchHarvestProject(
	baseUrl: string,
	projectId: string
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}`)
	);
}

export type HarvestFreshCopyRequest = {
	request_id: string;
	expected_revision: number;
	operator: string;
};

export async function createHarvestFreshCopy(
	baseUrl: string,
	projectId: string,
	payload: HarvestFreshCopyRequest
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/fresh-copy`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload),
			signal: AbortSignal.timeout(30000)
		})
	);
}

export async function createHarvestProject(
	baseUrl: string,
	draftId: string
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(`${baseUrl}/api/project-harvest/projects`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ draft_id: draftId })
		})
	);
}

export async function uploadHarvestBom(
	baseUrl: string,
	projectId: string,
	file: File,
	provider: string
): Promise<HarvestProject> {
	const query = new URLSearchParams({ filename: file.name, provider });
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/bom?${query.toString()}`,
			{
				method: 'POST',
				headers: {
					'Content-Type': file.name.toLowerCase().endsWith('.csv') ? 'text/csv' : 'application/json'
				},
				body: file
			}
		)
	);
}

export async function fetchHarvestBomFromRebrickable(
	baseUrl: string,
	projectId: string
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/bom/rebrickable`,
			{ method: 'POST' }
		)
	);
}

export async function updateHarvestReview(
	baseUrl: string,
	projectId: string,
	payload: Record<string, unknown>
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/review`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload)
		})
	);
}

export async function planHarvestCapacity(
	baseUrl: string,
	projectId: string,
	assignmentOverrides?: Array<{ group_id: string; bin_id: string }>
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/capacity-plan`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(
					assignmentOverrides ? { assignment_overrides: assignmentOverrides } : {}
				)
			}
		)
	);
}

export async function fetchHarvestBinReadiness(
	baseUrl: string,
	projectId: string
): Promise<HarvestBinReadiness> {
	return unwrap<HarvestBinReadiness>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/bin-readiness`
		)
	);
}

export async function clearHarvestBins(
	baseUrl: string,
	projectId: string,
	payload: {
		bin_ids: string[];
		expected_bin_state_token: string;
		operator: string;
		physical_bins_emptied: boolean;
	}
): Promise<{
	project: HarvestProject;
	bin_readiness: HarvestBinReadiness;
	clearance: Record<string, unknown>;
}> {
	return unwrap(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/bin-clearance`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function simulateHarvestProject(
	baseUrl: string,
	projectId: string
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/simulate`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({})
			}
		)
	);
}

export async function proposeHarvestAllocation(
	baseUrl: string,
	projectId: string,
	payload: { piece_id: string; part_id: string; color_id: string; quantity?: number }
): Promise<Record<string, unknown>> {
	return unwrap<Record<string, unknown>>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/allocations`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ ...payload, mode: 'simulation' })
			}
		)
	);
}

export async function confirmHarvestAllocation(
	baseUrl: string,
	projectId: string,
	allocationId: string
): Promise<Record<string, unknown>> {
	return unwrap<Record<string, unknown>>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/allocations/${encodeURIComponent(allocationId)}/confirm`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ evidence: { simulation: true } })
			}
		)
	);
}

export async function undoHarvestAllocation(
	baseUrl: string,
	projectId: string,
	allocationId: string,
	reason: string
): Promise<HarvestAllocation> {
	return unwrap<HarvestAllocation>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/allocations/${encodeURIComponent(allocationId)}/undo`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ reason })
			}
		)
	);
}

export async function transitionHarvestProject(
	baseUrl: string,
	projectId: string,
	target: 'review' | 'paused' | 'completed',
	reason: string
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/transition`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ target, reason })
			}
		)
	);
}

export async function startHarvestAcceptanceRun(
	baseUrl: string,
	projectId: string,
	payload: {
		expected_revision: number;
		expected_bin_state_token: string;
		test_id: string;
		operator: string;
		test_piece_limit: number;
		physical_bins_verified_empty: boolean;
	}
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/acceptance-run/start`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function fetchHarvestAcceptancePieces(
	baseUrl: string,
	projectId: string,
	acceptanceRunId?: string | null
): Promise<HarvestAcceptancePiecesResponse> {
	const params = new URLSearchParams();
	if (acceptanceRunId) params.set('acceptance_run_id', acceptanceRunId);
	const query = params.size > 0 ? `?${params.toString()}` : '';
	return unwrap<HarvestAcceptancePiecesResponse>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/acceptance-pieces${query}`
		)
	);
}

export async function finishHarvestAcceptanceRun(
	baseUrl: string,
	projectId: string,
	payload: {
		acceptance_run_id: string;
		operator: string;
		result: 'passed' | 'failed';
		observed_destinations_match: boolean;
		notes: string;
	}
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/acceptance-run/finish`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function reopenHarvestAcceptanceRunAfterCorrection(
	baseUrl: string,
	projectId: string,
	payload: { acceptance_run_id: string; operator: string; reason: string }
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/acceptance-run/reopen-after-correction`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function abortHarvestAcceptanceRun(
	baseUrl: string,
	projectId: string,
	payload: { acceptance_run_id: string; operator: string; reason: string }
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/acceptance-run/abort`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function recordHarvestGreenLight(
	baseUrl: string,
	projectId: string,
	payload: {
		expected_revision: number;
		expected_bin_state_token: string;
		operator: string;
		reason: string;
		physical_bins_verified_empty: boolean;
	}
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/green-light`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function fetchHarvestRuntime(baseUrl: string): Promise<HarvestRuntimeStatus> {
	return unwrap<HarvestRuntimeStatus>(await fetch(`${baseUrl}/api/project-harvest/runtime`));
}

export async function activateHarvestProject(
	baseUrl: string,
	projectId: string,
	payload: {
		expected_revision: number;
		expected_bin_state_token: string;
		operator: string;
		reason: string;
	}
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/activate`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export async function deactivateHarvestProject(
	baseUrl: string,
	projectId: string,
	payload: { operator: string; reason: string }
): Promise<HarvestProject> {
	return unwrap<HarvestProject>(
		await fetch(
			`${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/deactivate`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload)
			}
		)
	);
}

export function missingPartsCsvUrl(baseUrl: string, projectId: string): string {
	return `${baseUrl}/api/project-harvest/projects/${encodeURIComponent(projectId)}/missing-parts.csv`;
}
