export class SingleFlight {
  private readonly active = new Map<string, Promise<unknown>>();

  run<T>(key: string, action: () => Promise<T>): Promise<T> {
    const existing = this.active.get(key);
    if (existing) return existing as Promise<T>;

    const promise = action().finally(() => {
      if (this.active.get(key) === promise) this.active.delete(key);
    });
    this.active.set(key, promise);
    return promise;
  }
}

export class InjectionRegistry {
  private readonly mounted = new Set<string>();

  ensure(target: string, inject: () => void): boolean {
    if (this.mounted.has(target)) return false;
    inject();
    this.mounted.add(target);
    return true;
  }

  removed(target: string): void {
    this.mounted.delete(target);
  }
}
