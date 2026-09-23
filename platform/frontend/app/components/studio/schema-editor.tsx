import { HugeiconsIcon } from "@hugeicons/react";
import { Add01Icon, Delete02Icon, Key01Icon } from "@hugeicons/core-free-icons";

import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "~/components/ui/field";
import { Input } from "~/components/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "~/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
import type { ScenarioConfiguration, SchemaTable } from "~/lib/studio-types";

const fieldTypes = ["text", "integer", "float", "boolean", "date", "datetime", "email", "foreign_key"];

function fieldType(field: unknown): string {
  if (typeof field === "string") return field;
  if (field && typeof field === "object" && "type" in field && typeof field.type === "string") {
    return field.type;
  }
  return "text";
}

function fieldConstraints(field: unknown): Record<string, string | number | boolean> {
  if (
    field &&
    typeof field === "object" &&
    "constraints" in field &&
    field.constraints &&
    typeof field.constraints === "object"
  ) {
    return field.constraints as Record<string, string | number | boolean>;
  }
  return {};
}

export function SchemaEditor({
  scenario,
  onChange,
}: {
  scenario: ScenarioConfiguration;
  onChange: (scenario: ScenarioConfiguration) => void;
}) {
  const schemas = scenario.schemas ?? {};
  const tableNames = Object.keys(schemas);

  const updateTable = (tableName: string, table: SchemaTable) => {
    onChange({ ...scenario, schemas: { ...schemas, [tableName]: table } });
  };

  const renameField = (tableName: string, oldName: string, newName: string) => {
    const cleanName = newName.trim().replace(/\s+/g, "_");
    if (!cleanName || cleanName === oldName || cleanName in schemas[tableName]) return;
    const nextEntries = Object.entries(schemas[tableName]).map(([name, definition]) =>
      name === oldName ? [cleanName, definition] : [name, definition],
    );
    const nextTable = Object.fromEntries(nextEntries) as SchemaTable;
    const foreignKeys = nextTable.__foreign_keys__ as Record<string, string> | undefined;
    if (foreignKeys && oldName in foreignKeys) {
      nextTable.__foreign_keys__ = {
        ...foreignKeys,
        [cleanName]: foreignKeys[oldName],
      };
      delete (nextTable.__foreign_keys__ as Record<string, string>)[oldName];
    }
    updateTable(tableName, nextTable);
  };

  const updateFieldType = (tableName: string, fieldName: string, type: string) => {
    const table = schemas[tableName];
    const current = table[fieldName];
    const constraints = fieldConstraints(current);
    updateTable(tableName, {
      ...table,
      [fieldName]: Object.keys(constraints).length ? { type, constraints } : type,
    });
  };

  const addField = (tableName: string) => {
    const table = schemas[tableName];
    let index = Object.keys(table).filter((name) => !name.startsWith("__")).length + 1;
    let name = `field_${index}`;
    while (name in table) name = `field_${++index}`;
    updateTable(tableName, { ...table, [name]: "text" });
  };

  const removeField = (tableName: string, fieldName: string) => {
    const table = { ...schemas[tableName] };
    delete table[fieldName];
    const foreignKeys = table.__foreign_keys__ as Record<string, string> | undefined;
    if (foreignKeys) {
      const nextForeignKeys = { ...foreignKeys };
      delete nextForeignKeys[fieldName];
      table.__foreign_keys__ = nextForeignKeys;
    }
    updateTable(tableName, table);
  };

  if (!tableNames.length) {
    return (
      <p className="text-xs text-muted-foreground">
        No table schemas are defined. Ask the agent to add entities and fields.
      </p>
    );
  }

  return (
    <Tabs defaultValue={tableNames[0]}>
      <TabsList variant="line" className="max-w-full overflow-x-auto">
        {tableNames.map((tableName) => (
          <TabsTrigger key={tableName} value={tableName}>
            {tableName}
            <Badge variant="outline">
              {Object.keys(schemas[tableName]).filter((name) => !name.startsWith("__")).length}
            </Badge>
          </TabsTrigger>
        ))}
      </TabsList>
      {tableNames.map((tableName) => {
        const table = schemas[tableName];
        const foreignKeys = (table.__foreign_keys__ ?? {}) as Record<string, string>;
        const fields = Object.entries(table).filter(([name]) => !name.startsWith("__"));
        return (
          <TabsContent className="flex flex-col gap-3 pt-3" key={tableName} value={tableName}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Constraints</TableHead>
                  <TableHead className="w-10"><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fields.map(([name, definition]) => {
                  const constraints = fieldConstraints(definition);
                  return (
                    <TableRow key={name}>
                      <TableCell>
                        <FieldGroup>
                          <Field>
                            <FieldLabel className="sr-only" htmlFor={`${tableName}-${name}`}>Field name</FieldLabel>
                            <Input
                              defaultValue={name}
                              id={`${tableName}-${name}`}
                              onBlur={(event) => renameField(tableName, name, event.target.value)}
                            />
                          </Field>
                        </FieldGroup>
                      </TableCell>
                      <TableCell>
                        <Select value={fieldType(definition)} onValueChange={(value) => updateFieldType(tableName, name, value ?? "text")}>
                          <SelectTrigger className="w-36" aria-label={`Type for ${name}`}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectGroup>
                              {fieldTypes.map((type) => <SelectItem key={type} value={type}>{type}</SelectItem>)}
                            </SelectGroup>
                          </SelectContent>
                        </Select>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {constraints.primary_key && (
                            <Badge variant="secondary"><HugeiconsIcon data-icon="inline-start" icon={Key01Icon} strokeWidth={2} />Primary key</Badge>
                          )}
                          {foreignKeys[name] && <Badge variant="outline">FK → {foreignKeys[name]}</Badge>}
                          {!constraints.primary_key && !foreignKeys[name] && <span className="text-muted-foreground">—</span>}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Button aria-label={`Remove ${name}`} onClick={() => removeField(tableName, name)} size="icon-sm" type="button" variant="ghost">
                          <HugeiconsIcon icon={Delete02Icon} strokeWidth={2} />
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            <div className="flex items-center justify-between gap-3">
              <FieldDescription>{fields.length} fields in {tableName}</FieldDescription>
              <Button onClick={() => addField(tableName)} size="sm" type="button" variant="outline">
                <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} />
                Add field
              </Button>
            </div>
          </TabsContent>
        );
      })}
    </Tabs>
  );
}
