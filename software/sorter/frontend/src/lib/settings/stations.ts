import {
	Activity,
	Camera,
	CircuitBoard,
	Cloud,
	Cpu,
	Gauge,
	GitBranch,
	Layers3,
	Network,
	Settings,
	Shapes,
	ShieldAlert,
	SlidersHorizontal,
	Wrench,
	Zap
} from 'lucide-svelte';
import {
	CLASSIFICATION_CHANNEL_STEPPER_GEAR_RATIO,
	CLASSIFICATION_CHANNEL_STEPPER_LABEL
} from '$lib/settings/stepper-control';

export type CameraRole =
	| 'c_channel_2'
	| 'c_channel_3'
	| 'carousel'
	| 'classification_channel';

export type ZoneChannel =
	| 'second'
	| 'third'
	| 'carousel'
	| 'classification_channel';

export type StepperKey =
	| 'c_channel_1'
	| 'c_channel_2'
	| 'c_channel_3'
	| 'c_channel_4'
	| 'carousel'
	| 'chute';

export type EndstopConfig = {
	configEndpoint: string;
	liveEndpoint: string;
	homeEndpoint: string;
	homeCancelEndpoint: string;
	calibrateEndpoint?: string;
};

export type StationSlug =
	| 'c-channel-1'
	| 'c-channel-2'
	| 'c-channel-3'
	| 'classification-channel';

export type SettingsNavItem = {
	href: string;
	label: string;
	icon: typeof Settings;
};

export type StationPageConfig = SettingsNavItem & {
	slug: StationSlug;
	description: string;
	cameraRoles: CameraRole[];
	zoneChannels: ZoneChannel[];
	stepperKeys: StepperKey[];
	stepperEndstops?: Partial<Record<StepperKey, EndstopConfig>>;
	stepperDisplay?: Partial<Record<StepperKey, { label?: string; gearRatio?: number }>>;
};

export const generalNavItem: SettingsNavItem = {
	href: '/settings',
	label: 'General',
	icon: Settings
};

export const storageLayersNavItem: SettingsNavItem = {
	href: '/settings/storage-layers',
	label: 'Storage Layers',
	icon: Layers3
};

export const hiveNavItem: SettingsNavItem = {
	href: '/settings/hive',
	label: 'Hive',
	icon: Cloud
};

export const hiveModelsNavItem: SettingsNavItem = {
	href: '/settings/hive/models',
	label: 'Local Models',
	icon: Cpu
};

export const classificationProvidersNavItem: SettingsNavItem = {
	href: '/settings/providers',
	label: 'Providers',
	icon: Network
};

export const versionsNavItem: SettingsNavItem = {
	href: '/settings/versions',
	label: 'Versions',
	icon: GitBranch
};

export const chuteNavItem: SettingsNavItem = {
	href: '/settings/chute',
	label: 'Chute',
	icon: Wrench
};

export const controlBoardNavItem: SettingsNavItem = {
	href: '/settings/control-board',
	label: 'Control Board',
	icon: CircuitBoard
};

export const chuteAimingNavItem: SettingsNavItem = {
	href: '/settings/chute-aiming',
	label: 'Chute Aiming',
	icon: Shapes
};

export const stallguardNavItem: SettingsNavItem = {
	href: '/settings/stepper-stallguard',
	label: 'StallGuard',
	icon: Activity
};

export const jitterTestNavItem: SettingsNavItem = {
	href: '/settings/jitter-test',
	label: 'Jitter Test',
	icon: Zap
};

export const performanceNavItem: SettingsNavItem = {
	href: '/settings/performance',
	label: 'Performance',
	icon: Gauge
};

export const incidentsNavItem: SettingsNavItem = {
	href: '/settings/incidents',
	label: 'Incidents',
	icon: ShieldAlert
};

export const powerStressNavItem: SettingsNavItem = {
	href: '/settings/power-stress',
	label: 'Power Stress Test',
	icon: Zap
};

export const tuningNavItems: SettingsNavItem[] = [
	{
		href: '/settings/tuning/feeder-pulse-perception',
		label: 'Feeder Simple Pulse',
		icon: SlidersHorizontal
	},
	{
		href: '/settings/tuning/classification-channel',
		label: 'Classification Channel',
		icon: SlidersHorizontal
	},
	{
		href: '/settings/tuning/object-tracker',
		label: 'Object Tracker',
		icon: SlidersHorizontal
	},
	{
		href: '/settings/tuning/piece-link',
		label: 'Piece Link (experimental)',
		icon: SlidersHorizontal
	}
];

export const stationPageConfigs: StationPageConfig[] = [
	{
		slug: 'c-channel-1',
		href: '/settings/c-channel-1',
		label: 'C-Channel 1',
		icon: Wrench,
		description: 'Bulk feed channel. This station only exposes manual stepper control.',
		cameraRoles: [],
		zoneChannels: [],
		stepperKeys: ['c_channel_1']
	},
	{
		slug: 'c-channel-2',
		href: '/settings/c-channel-2',
		label: 'C-Channel 2',
		icon: Camera,
		description: 'Configure the second feeder camera, zone geometry, and rotor stepper controls.',
		cameraRoles: ['c_channel_2'],
		zoneChannels: ['second'],
		stepperKeys: ['c_channel_2']
	},
	{
		slug: 'c-channel-3',
		href: '/settings/c-channel-3',
		label: 'C-Channel 3',
		icon: Camera,
		description: 'Configure the third feeder camera, zone geometry, and rotor stepper controls.',
		cameraRoles: ['c_channel_3'],
		zoneChannels: ['third'],
		stepperKeys: ['c_channel_3']
	},
	{
		slug: 'classification-channel',
		href: '/settings/classification-channel',
		label: 'Classification C-Channel (C4)',
		icon: Camera,
		description:
			'Configure the fourth C-channel camera, arc zones, and classification-channel stepper.',
		cameraRoles: ['classification_channel'],
		zoneChannels: ['classification_channel'],
		stepperKeys: ['c_channel_4'],
		stepperDisplay: {
			c_channel_4: {
				label: CLASSIFICATION_CHANNEL_STEPPER_LABEL,
				gearRatio: CLASSIFICATION_CHANNEL_STEPPER_GEAR_RATIO
			}
		}
	}
];

export type SettingsNavHeading = {
	type: 'heading';
	label: string;
};

export type SettingsNavEntry = SettingsNavItem | SettingsNavHeading;

export const settingsNavItems: SettingsNavEntry[] = [
	generalNavItem,
	hiveNavItem,
	hiveModelsNavItem,
	classificationProvidersNavItem,
	versionsNavItem,
	{ type: 'heading', label: 'Hardware' },
	...stationPageConfigs,
	chuteNavItem,
	storageLayersNavItem,
	controlBoardNavItem,
	{ type: 'heading', label: 'Helpers' },
	incidentsNavItem,
	powerStressNavItem,
	chuteAimingNavItem,
	stallguardNavItem,
	jitterTestNavItem,
	performanceNavItem,
	{ type: 'heading', label: 'Tuning' },
	...tuningNavItems
];

export function getStationPageConfig(slug: string): StationPageConfig | undefined {
	return stationPageConfigs.find((station) => station.slug === slug);
}

export const stepperLabels: Record<StepperKey, string> = {
	c_channel_1: 'C Channel 1',
	c_channel_2: 'C Channel 2',
	c_channel_3: 'C Channel 3',
	c_channel_4: 'C Channel 4',
	carousel: CLASSIFICATION_CHANNEL_STEPPER_LABEL,
	chute: 'Chute'
};
