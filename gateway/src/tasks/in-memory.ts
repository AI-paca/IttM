import type { IdGenerator, TaskQueue, TaskRecord, TaskStore } from "./types";

export class SequentialIds implements IdGenerator {
  private current = 0;

  next(): string {
    this.current += 1;
    return `task-${this.current}`;
  }
}

export class InMemoryTaskQueue implements TaskQueue {
  private readonly ids: string[] = [];

  get size(): number {
    return this.ids.length;
  }

  enqueue(id: string): void {
    this.ids.push(id);
  }

  dequeue(): string | undefined {
    return this.ids.shift();
  }

  remove(id: string): boolean {
    const index = this.ids.indexOf(id);
    if (index < 0) return false;
    this.ids.splice(index, 1);
    return true;
  }
}

export class InMemoryTaskStore implements TaskStore {
  private readonly records = new Map<string, TaskRecord>();

  get(id: string): TaskRecord | undefined {
    return this.records.get(id);
  }

  put(record: TaskRecord): void {
    this.records.set(record.id, record);
  }

  list(): TaskRecord[] {
    return Array.from(this.records.values());
  }
}
