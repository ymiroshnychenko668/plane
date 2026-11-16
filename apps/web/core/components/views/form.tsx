/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useRef, useState } from "react";
import { observer } from "mobx-react";
import { Controller, useForm } from "react-hook-form";
// plane imports
import { ETabIndices, ISSUE_DISPLAY_FILTERS_BY_PAGE } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { EmojiPicker, EmojiIconPickerTypes, Logo } from "@plane/propel/emoji-icon-picker";
import { ViewsIcon } from "@plane/propel/icons";
import type {
  IIssueDisplayFilterOptions,
  IIssueDisplayProperties,
  IProjectView,
  EIssueLayoutTypes,
  IIssueFilters,
} from "@plane/types";
import { EViewAccess, EIssuesStoreType } from "@plane/types";
import { Input, TextArea } from "@plane/ui";
import { getComputedDisplayFilters, getComputedDisplayProperties, getTabIndex } from "@plane/utils";
// components
import { DisplayFiltersSelection, FiltersDropdown } from "@/components/issues/issue-layouts/filters";
import { WorkItemFiltersRow } from "@/components/work-item-filters/filters-row";
// hooks
import { useProject } from "@/hooks/store/use-project";
import { usePlatformOS } from "@/hooks/use-platform-os";
// local imports
import { LayoutDropDown } from "../dropdowns/layout";
import { ProjectLevelWorkItemFiltersHOC } from "../work-item-filters/filters-hoc/project-level";
import { createScenarioDefinition, isScenarioView, readScenario, viewErrorMessage } from "./scenario";
import type { TViewScenario } from "./scenario";
import { ScenarioViewResults } from "./scenario-results";

type Props = {
  data?: IProjectView | null;
  handleClose: () => void;
  handleFormSubmit: (values: IProjectView) => Promise<void>;
  preLoadedData?: Partial<IProjectView> | null;
  projectId: string;
  workspaceSlug: string;
};

const DEFAULT_VALUES: Partial<IProjectView> = {
  name: "",
  description: "",
  access: EViewAccess.PUBLIC,
  display_properties: getComputedDisplayProperties(),
  display_filters: { ...getComputedDisplayFilters(), group_by: "state" },
};

export const ProjectViewForm = observer(function ProjectViewForm(props: Props) {
  const { handleFormSubmit, handleClose, data: incomingData, preLoadedData, projectId, workspaceSlug } = props;
  const data = useRef(incomingData).current;
  // i18n
  const { t } = useTranslation();
  // state
  const [isOpen, setIsOpen] = useState(false);
  const originalScenario = readScenario(data ?? preLoadedData ?? {});
  const unsupported = isScenarioView(data ?? preLoadedData ?? {}) && !originalScenario;
  const [scenario, setScenario] = useState<TViewScenario | "custom">(originalScenario?.scenario ?? "custom");
  const [days, setDays] = useState(originalScenario?.days ?? 7);
  const [preview, setPreview] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const lastRequest = useRef<{ payload: string; id: string } | null>(null);
  const validDays = scenario === "unassigned" || (Number.isInteger(days) && days >= 1 && days <= 36500);
  const scenarioDefinition = scenario !== "custom" && validDays ? createScenarioDefinition(scenario, days) : null;
  // store hooks
  const { getProjectById } = useProject();
  const { isMobile } = usePlatformOS();
  // form info
  const defaultValues = {
    ...DEFAULT_VALUES,
    ...preLoadedData,
    ...data,
  };
  const {
    control,
    formState: { errors, isSubmitting },
    getValues,
    handleSubmit,
    reset,
    setValue,
    watch,
  } = useForm<IProjectView>({
    defaultValues,
  });
  // derived values
  const projectDetails = getProjectById(projectId);
  const logoValue = watch("logo_props");
  const workItemFilters: IIssueFilters = {
    richFilters: getValues("rich_filters"),
    displayFilters: getValues("display_filters"),
    displayProperties: getValues("display_properties"),
    kanbanFilters: undefined,
  };
  const { getIndex } = getTabIndex(ETabIndices.PROJECT_VIEW, isMobile);

  const handleCreateUpdateView = async (formData: IProjectView) => {
    if (unsupported || !validDays) return;
    setSubmitError(null);
    try {
      const payload = {
        name: formData.name,
        description: formData.description,
        ...(scenarioDefinition ? {} : { logo_props: formData.logo_props }),
        rich_filters: formData.rich_filters,
        display_filters: formData.display_filters,
        display_properties: formData.display_properties,
        access: formData.access,
        ...scenarioDefinition,
        ...(scenarioDefinition
          ? {
              display_filters: data?.display_filters ?? { layout: "list", order_by: "-created_at" },
              display_properties: data?.display_properties,
            }
          : {}),
        revision: data?.revision,
      } as IProjectView;
      if (scenarioDefinition && !data) {
        const serialized = JSON.stringify(payload);
        if (lastRequest.current?.payload !== serialized)
          lastRequest.current = { payload: serialized, id: crypto.randomUUID() };
        payload.request_id = lastRequest.current.id;
      }
      await handleFormSubmit(payload);

      reset({
        ...defaultValues,
      });
    } catch (error) {
      setSubmitError(viewErrorMessage(error));
    }
  };

  return (
    <form onSubmit={handleSubmit(handleCreateUpdateView)}>
      <div className="space-y-5 p-5">
        <h3 className="text-18 font-medium text-secondary">{data ? t("view.update.label") : t("view.create.label")}</h3>
        <div className="space-y-3">
          <div className="flex w-full items-start gap-2">
            {scenario === "custom" && !unsupported && (
              <EmojiPicker
                iconType="lucide"
                isOpen={isOpen}
                handleToggle={(val: boolean) => setIsOpen(val)}
                className="flex-shrink0 flex items-center justify-center"
                buttonClassName="flex items-center justify-center"
                label={
                  <span className="grid h-9 w-9 place-items-center rounded-md bg-surface-2">
                    <>
                      {logoValue?.in_use ? (
                        <Logo logo={logoValue} size={18} type="lucide" />
                      ) : (
                        <ViewsIcon className="h-4 w-4 text-tertiary" />
                      )}
                    </>
                  </span>
                }
                // TODO: fix types
                onChange={(val: any) => {
                  // oxlint-disable-next-line no-shadow
                  let logoValue = {};

                  if (val?.type === "emoji")
                    logoValue = {
                      value: val.value,
                    };
                  else if (val?.type === "icon") logoValue = val.value;

                  setValue("logo_props", {
                    in_use: val?.type,
                    [val?.type]: logoValue,
                  });
                  setIsOpen(false);
                }}
                defaultIconColor={
                  logoValue?.in_use && logoValue?.in_use === "icon" ? logoValue?.icon?.color : undefined
                }
                defaultOpen={
                  logoValue?.in_use && logoValue?.in_use === "emoji"
                    ? EmojiIconPickerTypes.EMOJI
                    : EmojiIconPickerTypes.ICON
                }
              />
            )}
            <div className="flew-grow w-full space-y-1">
              <Controller
                control={control}
                name="name"
                rules={{
                  required: t("form.title.required"),
                  maxLength: {
                    value: 255,
                    message: t("form.title.max_length", { length: 255 }),
                  },
                }}
                render={({ field: { value, onChange } }) => (
                  <Input
                    id="name"
                    type="name"
                    name="name"
                    value={value}
                    onChange={onChange}
                    hasError={Boolean(errors.name)}
                    placeholder={t("common.title")}
                    className="w-full text-14"
                    tabIndex={getIndex("name")}
                    // oxlint-disable-next-line jsx_a11y/no-autofocus
                    autoFocus
                  />
                )}
              />
              <span className="text-11 text-danger-primary">{errors?.name?.message?.toString()}</span>
            </div>
          </div>
          <div>
            <Controller
              name="description"
              control={control}
              render={({ field: { value, onChange } }) => (
                <TextArea
                  id="description"
                  name="description"
                  placeholder={t("common.description")}
                  className="min-h-24 w-full resize-none text-14"
                  hasError={Boolean(errors?.description)}
                  value={value}
                  onChange={onChange}
                  tabIndex={getIndex("descriptions")}
                />
              )}
            />
          </div>
          {(!data || originalScenario || unsupported) && (
            <div className="space-y-3 rounded-sm border border-subtle p-3">
              <label className="block" htmlFor="project-view-scenario">
                View condition
                <select
                  id="project-view-scenario"
                  className="mt-1 w-full rounded-sm border border-subtle bg-surface-1 p-2"
                  value={scenario}
                  disabled={unsupported}
                  onChange={(event) => {
                    setScenario(event.target.value as TViewScenario | "custom");
                    setPreview(false);
                  }}
                >
                  {!originalScenario && <option value="custom">Custom filters</option>}
                  <option value="inactive">No meaningful activity for X days</option>
                  <option value="unassigned">No assignee</option>
                  <option value="inactive_unassigned">No meaningful activity and no assignee</option>
                </select>
              </label>
              {scenario !== "custom" && (
                <>
                  {scenario !== "unassigned" && (
                    <label className="block" htmlFor="project-view-days">
                      Days without meaningful activity
                      <input
                        id="project-view-days"
                        type="number"
                        min={1}
                        max={36500}
                        step={1}
                        required
                        className="mt-1 w-full rounded-sm border border-subtle bg-surface-1 p-2"
                        value={Number.isNaN(days) ? "" : days}
                        onChange={(event) => {
                          setDays(event.target.valueAsNumber);
                          setPreview(false);
                        }}
                      />
                    </label>
                  )}
                  <p className="text-12 text-tertiary">
                    Open work items only; archived items and drafts are excluded. These conditions use the list layout.
                  </p>
                  {scenario !== "unassigned" && (
                    <p className="text-12 text-tertiary">
                      Meaningful activity is reviewed evidence from comments and descriptions, including investigation,
                      decisions and results. Missing or uncertain evidence appears separately as needs review.
                    </p>
                  )}
                  {!validDays && (
                    <p role="alert" className="text-danger-primary">
                      Enter a whole number from 1 to 36500.
                    </p>
                  )}
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    disabled={!validDays}
                    onClick={() => setPreview(!preview)}
                  >
                    {preview ? "Hide preview" : "Preview work items"}
                  </Button>
                  {preview && scenarioDefinition && (
                    <ScenarioViewResults
                      key={JSON.stringify(scenarioDefinition)}
                      workspaceSlug={workspaceSlug}
                      projectId={projectId}
                      definition={scenarioDefinition}
                    />
                  )}
                </>
              )}
              {unsupported && (
                <>
                  <p role="alert">
                    This filter or policy version is unsupported. Its raw definition is preserved and saving is
                    disabled.
                  </p>
                  <pre className="max-h-48 overflow-auto text-12 break-words whitespace-pre-wrap">
                    {JSON.stringify(
                      {
                        rich_filters: data?.rich_filters ?? preLoadedData?.rich_filters,
                        filter_meta: data?.filter_meta ?? preLoadedData?.filter_meta,
                      },
                      null,
                      2
                    )}
                  </pre>
                </>
              )}
            </div>
          )}
          {scenario === "custom" && !unsupported && (
            <div className="flex gap-2">
              <Controller
                control={control}
                name="display_filters"
                render={({ field: { onChange: onDisplayFiltersChange, value: displayFilters } }) => (
                  <>
                    {/* layout dropdown */}
                    <LayoutDropDown
                      onChange={(selectedValue: EIssueLayoutTypes) =>
                        onDisplayFiltersChange({
                          ...displayFilters,
                          layout: selectedValue,
                        })
                      }
                      value={displayFilters.layout}
                    />
                    {/* display filters dropdown */}
                    <Controller
                      control={control}
                      name="display_properties"
                      render={({ field: { onChange: onDisplayPropertiesChange, value: displayProperties } }) => (
                        <FiltersDropdown title={t("common.display")}>
                          <DisplayFiltersSelection
                            layoutDisplayFiltersOptions={
                              ISSUE_DISPLAY_FILTERS_BY_PAGE.issues.layoutOptions[displayFilters.layout]
                            }
                            displayFilters={displayFilters ?? {}}
                            handleDisplayFiltersUpdate={(updatedDisplayFilter: Partial<IIssueDisplayFilterOptions>) => {
                              onDisplayFiltersChange({
                                ...displayFilters,
                                ...updatedDisplayFilter,
                              });
                            }}
                            displayProperties={displayProperties ?? {}}
                            handleDisplayPropertiesUpdate={(
                              updatedDisplayProperties: Partial<IIssueDisplayProperties>
                            ) => {
                              onDisplayPropertiesChange({
                                ...displayProperties,
                                ...updatedDisplayProperties,
                              });
                            }}
                            cycleViewDisabled={!projectDetails?.cycle_view}
                            moduleViewDisabled={!projectDetails?.module_view}
                          />
                        </FiltersDropdown>
                      )}
                    />
                  </>
                )}
              />
            </div>
          )}
          {scenario === "custom" && !unsupported && (
            <div>
              {/* filters dropdown */}
              <Controller
                control={control}
                name="rich_filters"
                render={({ field: { onChange: onFiltersChange } }) => (
                  <ProjectLevelWorkItemFiltersHOC
                    entityId={data?.id}
                    entityType={EIssuesStoreType.PROJECT_VIEW}
                    filtersToShowByLayout={ISSUE_DISPLAY_FILTERS_BY_PAGE.issues.filters}
                    initialWorkItemFilters={workItemFilters}
                    isTemporary
                    updateFilters={(updateFilters) => onFiltersChange(updateFilters)}
                    projectId={projectId}
                    showOnMount
                    workspaceSlug={workspaceSlug}
                  >
                    {({ filter: projectViewWorkItemsFilter }) =>
                      projectViewWorkItemsFilter && (
                        <WorkItemFiltersRow filter={projectViewWorkItemsFilter} variant="modal" />
                      )
                    }
                  </ProjectLevelWorkItemFiltersHOC>
                )}
              />
            </div>
          )}
          {submitError && (
            <p role="alert" className="text-danger-primary">
              {submitError}
            </p>
          )}
        </div>
      </div>
      <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
        <Button variant="secondary" size="lg" onClick={handleClose} tabIndex={getIndex("cancel")}>
          {t("common.cancel")}
        </Button>
        <Button
          variant="primary"
          size="lg"
          type="submit"
          tabIndex={getIndex("submit")}
          loading={isSubmitting}
          disabled={unsupported || !validDays}
        >
          {data
            ? isSubmitting
              ? t("common.updating")
              : t("view.update.label")
            : isSubmitting
              ? t("common.creating")
              : t("view.create.label")}
        </Button>
      </div>
    </form>
  );
});
