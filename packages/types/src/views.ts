/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TLogoProps } from "./common";
import type {
  IIssueDisplayFilterOptions,
  IIssueDisplayProperties,
  IIssueFilterOptions,
  TWorkItemFilterExpression,
} from "./view-props";

export enum EViewAccess {
  PRIVATE,
  PUBLIC,
}

export interface IProjectView {
  id: string;
  access: EViewAccess;
  created_at: Date;
  updated_at: Date;
  is_favorite: boolean;
  created_by: string;
  updated_by: string;
  name: string;
  description: string;
  rich_filters: TWorkItemFilterExpression;
  /** Versioned project View definitions bypass the legacy filter adapter. */
  filter_meta?: Record<string, unknown>;
  revision?: number;
  request_id?: string;
  display_filters: IIssueDisplayFilterOptions;
  display_properties: IIssueDisplayProperties;
  query: IIssueFilterOptions;
  query_data: IIssueFilterOptions;
  project: string;
  workspace: string;
  logo_props: TLogoProps | undefined;
  is_locked: boolean;
  anchor?: string;
  owned_by: string;
}

export type TProjectViewPartition = "matches" | "needs_review";

export type TProjectViewWorkItem = {
  id: string;
  name: string;
  sequence_id: number;
  project_id: string;
  state_id: string;
  priority: string;
  assignee_ids: string[];
  activity?: { match: boolean | null; last_meaningful_at: string | null; reason: string };
};

export type TProjectViewEvaluation = {
  results: TProjectViewWorkItem[];
  next_page_results: boolean;
  next_cursor: string | null;
  evaluated_at: string;
  cutoff_at: string | null;
  policy_version: string | null;
  view_revision: number | null;
  counts: { matches: number; needs_review: number };
  counts_observed_at: string;
  assessment_complete: boolean;
  consistency: "live";
};

export interface IPublishedProjectView extends Omit<IProjectView, "rich_filters"> {
  filters: IIssueFilterOptions;
}

export type TPublishViewSettings = {
  is_comments_enabled: boolean;
  is_reactions_enabled: boolean;
  is_votes_enabled: boolean;
};

export type TPublishViewDetails = TPublishViewSettings & {
  id: string;
  anchor: string;
};

export type TViewFiltersSortKey = "name" | "created_at" | "updated_at";

export type TViewFiltersSortBy = "asc" | "desc";

export type TViewFilterProps = {
  created_at?: string[] | null;
  owned_by?: string[] | null;
  favorites?: boolean;
  view_type?: EViewAccess[];
};

export type TViewFilters = {
  searchQuery: string;
  sortKey: TViewFiltersSortKey;
  sortBy: TViewFiltersSortBy;
  filters?: TViewFilterProps;
};
