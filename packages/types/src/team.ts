/** Project team registry shared by the browser and management API. */
export type TTeamRuntime = "codex" | "hermes";
export type TTeamTarget = {
  thread_id?: string;
  host_id?: string;
  agent_id?: string;
  profile?: string;
  conversation_id?: string;
};
export type TTeamMember = {
  id: string;
  membership_id: string;
  display_name: string;
  role: number;
  is_active: boolean;
  is_bot: boolean;
};
export type TTeamActor = {
  id: string;
  registration_id: string;
  runtime: TTeamRuntime;
  agent_key: string;
  user_id: string | null;
  display_name: string;
  role: string | null;
  active: boolean;
  member: boolean;
  revision: number;
  can_manage: boolean;
  binding: TTeamTarget & { session_id?: string; binding_id?: string };
};
export type TTeamContact = {
  id: string;
  address: string;
  registration_id: string;
  runtime: TTeamRuntime;
  agent_key: string;
  user_id: string | null;
  target: TTeamTarget;
  active: boolean;
  available: boolean;
  revision: number;
  display_name: string;
  role: string | null;
  can_manage: boolean;
};
export type TProjectTeam = {
  project: { id: string; name: string; identifier: string };
  members: TTeamMember[];
  actors: TTeamActor[];
  contacts: TTeamContact[];
  legacy: { page_id: string | null; status: string };
  can_manage_members: boolean;
};
export type TTeamContactWrite = { registration_id: string; target: TTeamTarget; active: boolean };
export type TTeamActorCreate = {
  runtime: TTeamRuntime;
  agent_key: string;
  user_id: string;
  display_name: string;
  role: string;
  binding: TTeamActor["binding"];
};
