export interface NetworkAvailability {
  mainnet: boolean;
  testnet: boolean;
}

export interface SecretsProvider {
  /** Returns the {apiKey, apiSecret} for the given network (useTestnet=true -> testnet).
   * Falls back to a legacy flat credential shape when present. */
  getCredential(providerId: string, useTestnet?: boolean): Promise<Record<string, any> | null>;
  /** Stores {apiKey, apiSecret} into the network slot (useTestnet=true -> testnet),
   * preserving the other network's stored keys. */
  storeCredential(providerId: string, credentialJson: Record<string, any>, useTestnet?: boolean): Promise<void>;
  /** Which networks currently have keys stored — powers the header toggle's "add keys" prompt. */
  getConfiguredNetworks(providerId: string): Promise<NetworkAvailability>;
  deleteCredential(providerId: string): Promise<void>;
}
